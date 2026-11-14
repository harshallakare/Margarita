"""Purpose: Nutanix AHV TargetProvider: each disk is written over iSCSI into a vmigrate volume group, then
the VM is created with Prism v2 cloning its disks from those volume-group disks (instant, copy-on-write).

Usage (endpoint config; the same endpoint can be a source too. Password is a credential):
    type: nutanix
    prism_element: https://pe:9440            # v2: VM create / power / networks / containers
    prism_central: https://pc:9440            # v4: volume groups (needs PC >= 7.3, as the iSCSI source)
    username / password / verify_tls
    target_container: default-container       # storage container for the new disks
    network_map: {"VLAN-120": "vm-network"}   # source network -> AHV network name (or UUID)
    default_network: vm-network               # for unmapped networks (unset: unmapped is an error)
    preserve_mac: false
    power_on: true
    keep_volume_groups: false                 # migrations: keep the volume groups after the VM exists

How it works:
  disk     : volume group vmigrate-disk-<name> with one empty disk on target_container
  write    : this worker's iSCSI initiator is attached to the group, the LUN is logged in on the Data
             Services IP and written directly (zero chunks skipped: new disks are thin)
  OSMorph  : guestfish / virt-v2v run on the same local LUN
  VM       : Prism v2 POST /vms with vm_disk_clone from each volume-group disk (scsi.0..N in source
             order), NICs on the mapped networks, UEFI (+ secure boot) as the source; powered on
  migration: the volume groups are deleted once the VM exists (its disks are independent clones)
  replica  : the volume groups ARE the replica; syncs rewrite them over iSCSI; a deploy clones them
             into the new VM (no copy). Restore points are not implemented.
Requires on the worker: open-iscsi and root, and port 3260 to the Data Services IP (as the source).
Tested against fakes only; see target_api.py for the request shapes to verify on the first live run.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...core import cancel
from ...core.context import RunContext
from ...core.errors import ConfigError, ProviderError, SafetyGateError
from ...core.fileio import BlockDeviceWriter, RawFileReader
from ...core.interfaces import BootCheck, DiskHints, DiskReader, DiskWriter, TargetDiskRef, TargetProvider, VmRef
from ...ir import DiskBus, Firmware, VmSpec
from .iscsi import IscsiSession, block_size_of, map_devices
from .target_api import NutanixTargetApi, ensure_ours
from .v4 import PREFIX

MIB = 1 << 20
MARK = "[vmigrate]"
_MAP = re.compile(r"vmigrate-disks=([^\s]+)")


def vg_name(disk_name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", disk_name.lower()).strip("-")[:60] or "disk"
    return f"{PREFIX}disk-{slug}"


def round_mib(n: int) -> int:
    return -(-n // MIB) * MIB


def vm_body(spec: VmSpec, disks: list[TargetDiskRef], nets: list[str], *, macs: list[str | None]) -> dict[str, Any]:
    """Prism v2 VM create body; disks are cloned from the volume-group disks (vmdisk_uuid)."""
    vm_disks = [{"is_cdrom": False, "disk_address": {"device_bus": "scsi", "device_index": i},
                 "vm_disk_clone": {"disk_address": {"vmdisk_uuid": d.data["disk_ext"]}, "minimum_size": d.size_bytes}}
                for i, d in enumerate(disks)]
    nics = []
    for net, mac in zip(nets, macs):
        n: dict[str, Any] = {"network_uuid": net}
        if mac:
            n["mac_address"] = mac.lower()
        nics.append(n)
    boot: dict[str, Any] = {"disk_address": {"device_bus": "scsi", "device_index": 0}}
    body: dict[str, Any] = {"name": spec.name, "memory_mb": max(256, spec.memory_bytes >> 20),
                            "num_vcpus": spec.cpu.sockets, "num_cores_per_vcpu": spec.cpu.cores,
                            "vm_disks": vm_disks, "vm_nics": nics,
                            "description": f"{MARK} migrated from {spec.source_id} "
                                           f"vmigrate-disks={','.join(d.id for d in disks)}"[:1000]}
    if spec.firmware == Firmware.UEFI:
        boot["uefi_boot"] = True
        if spec.secure_boot:
            boot["secure_boot"] = True
            body["machine_type"] = "Q35"
    body["boot"] = boot
    return body


def attached_refs(vm: Mapping[str, Any]) -> list[str]:
    """Disk ids in device order: the vmigrate refs recorded in the description when the count matches,
    else the VM's own vmdisk UUIDs."""
    disks = sorted((d for d in vm.get("vm_disk_info") or [] if not d.get("is_cdrom")),
                   key=lambda d: ((d.get("disk_address") or {}).get("device_bus", ""),
                                  int((d.get("disk_address") or {}).get("device_index", 0))))
    m = _MAP.search(str(vm.get("description") or ""))
    refs = m.group(1).split(",") if m else []
    if refs and len(refs) == len(disks):
        return refs
    return [str((d.get("disk_address") or {}).get("vmdisk_uuid", "")) for d in disks]


class NutanixTarget(TargetProvider):
    type_name = "nutanix"
    platform_family = "kvm"

    def __init__(self, name: str, config: Mapping[str, Any], ctx: RunContext, api: Any = None,
                 iscsi_factory: Any = None, size_of: Any = None):
        super().__init__(name, config, ctx)
        self._api = api
        self._iscsi_factory = iscsi_factory or IscsiSession
        self._size_of = size_of or block_size_of
        self._cluster: str | None = None
        self._portal: str | None = None
        self._sessions: dict[str, Any] = {}           # vg -> logged-in session
        self._devs: dict[str, str] = {}
        self._lock = threading.Lock()
        self.poll = float(config.get("poll_interval", 5))

    # ------------------------------------------------------------------ plumbing
    @property
    def api(self) -> NutanixTargetApi:
        if self._api is None:
            import os

            from .client import PrismClient
            from .v4 import PrismV4
            for k in ("prism_element", "prism_central", "username"):
                if not self.config.get(k):
                    raise ConfigError(f"nutanix target {self.name}: set {k}")
            c = PrismClient(pe_url=self.config["prism_element"], pc_url=self.config["prism_central"],
                            username=self.config["username"],
                            password=self.config.get("password") or os.environ.get(self.config.get("password_env", ""), ""),
                            verify_tls=self.config.get("verify_tls", True))
            self._api = NutanixTargetApi(c, PrismV4(c.s, self.config["prism_central"]))
        return self._api

    @property
    def cluster(self) -> str:
        if self._cluster is None:
            self._cluster = self.config.get("target_cluster") or self.api.cluster_uuid()
        return str(self._cluster)

    @property
    def portal(self) -> str:
        if self._portal is None:
            ip = self.api.data_services_ip(self.cluster)
            if not ip:
                raise ConfigError("nutanix target: the cluster has no Data Services IP (needed for iSCSI writes)")
            self._portal = ip
        return self._portal

    def _container(self) -> str:
        want = self.config.get("target_container")
        if not want:
            raise ConfigError("nutanix target: set target_container (storage container for the new disks)")
        for c in self.api.containers():
            if want in (c["name"], c["uuid"]):
                return c["uuid"]
        raise ConfigError(f"nutanix target: storage container {want!r} not found")

    def _network(self, source_net: str, o: Mapping[str, Any]) -> str:
        want = (o.get("network_map") or {}).get(source_net) or o.get("default_network")
        if not want:
            raise ConfigError(f"nutanix: no network_map entry for source network {source_net!r} (and no default_network)")
        for n in self.api.networks():
            if want in (n["name"], n["uuid"]):
                return n["uuid"]
        raise ConfigError(f"nutanix: network {want!r} not found")

    def _opts(self, spec: VmSpec) -> dict[str, Any]:
        t = spec.extra.get("target_options") or {}
        return {**self.config, **t, "network_map": {**(self.config.get("network_map") or {}), **(t.get("network_map") or {})}}

    # ------------------------------------------------------------------ checks + lists
    def check_connection(self) -> list[str]:
        try:
            self.api.cluster_uuid()
        except Exception as e:  # noqa: BLE001
            return [f"cannot reach Prism Element: {e}"]
        p: list[str] = []
        try:
            if not self.api.data_services_ip(self.cluster):
                p.append("the cluster has no Data Services IP (iSCSI writes need one)")
        except Exception as e:  # noqa: BLE001
            p.append(f"Prism Central v4 API not usable (volume groups): {e}")
        try:
            self._container()
        except ConfigError as e:
            p.append(str(e))
        return p

    def preflight(self, spec: VmSpec | None = None) -> list[str]:
        p = self.check_connection()
        try:
            self._iscsi_factory(self._portal or "127.0.0.1").initiator_name()
        except Exception as e:  # noqa: BLE001
            p.append(str(e))
        if spec is None or p:
            return p
        o = self._opts(spec)
        for nic in spec.nics:
            try:
                self._network(nic.network, o)
            except ConfigError as e:
                p.append(str(e))
        return p

    def list_networks(self) -> list[dict[str, Any]]:
        return sorted(({"id": n["name"], "name": n["name"] + (f" (VLAN {n['vlan']})" if n.get("vlan") is not None else "")}
                       for n in self.api.networks()), key=lambda n: n["id"])

    def list_storage(self) -> list[dict[str, Any]]:
        return sorted(({"id": c["name"], "name": c["name"]} for c in self.api.containers()), key=lambda c: c["id"])

    # ------------------------------------------------------------------ disks
    def create_disk(self, name: str, size_bytes: int, hints: DiskHints) -> TargetDiskRef:
        if hints.shared:
            raise ConfigError("nutanix target: shared (multi-attach) cluster disks are not supported yet")
        vg = vg_name(name)
        size = round_mib(size_bytes)
        cont = self._container()
        self.ctx.require_execute("nutanix create volume group + disk", name=vg, size_bytes=size)
        ext = self.api.create_volume_group(vg, self.cluster)
        self.api.add_empty_disk(ext, size, cont)
        return self._ref(ext, vg, name)

    def _ref(self, ext: str, vg: str, name: str) -> TargetDiskRef:
        disks = self.api.vg_disks(ext)
        if len(disks) != 1:
            raise ProviderError(f"volume group {vg} has {len(disks)} disks; expected 1")
        d = disks[0]
        return TargetDiskRef(id=ext, name=name, size_bytes=int(d["size"]),
                             data={"vg": ext, "vg_name": vg, "disk_ext": d["ext"], "cluster": self.cluster})

    def find_disk(self, name: str) -> TargetDiskRef | None:
        vg = vg_name(name)
        ext = self.api.find_volume_group(vg)
        return self._ref(ext, vg, name) if ext else None

    def _device(self, disk: TargetDiskRef) -> str:
        vg = disk.data["vg"]
        with self._lock:
            if vg in self._devs:
                return self._devs[vg]
            ensure_ours(self.api.volume_group(vg), vg)
            sess = self._iscsi_factory(self.portal)
            iqn = sess.initiator_name()
            self.ctx.require_execute("nutanix attach iSCSI client", volume_group=disk.data["vg_name"], iqn=iqn)
            self.api.attach_iscsi_client(vg, iqn)
            sess.vg, sess.iqn = vg, iqn
            self._sessions[vg] = sess
            devs = sess.login(disk.data["vg_name"], expected=1)
            dev = map_devices([{"index": 0, "size": disk.size_bytes}], devs, self._size_of)[0]
            self._devs[vg] = dev
            return dev

    def _release(self, vg: str | None = None) -> None:
        for key in [k for k in list(self._sessions) if vg is None or k == vg]:
            sess = self._sessions.pop(key)
            self._devs.pop(key, None)
            try:
                sess.logout()
            finally:
                try:
                    self.api.detach_iscsi_client(key, getattr(sess, "iqn", ""))
                except Exception as e:  # noqa: BLE001 - keep releasing the rest
                    self.ctx.logger.warning("nutanix: detach iSCSI client from %s: %s", key, e)

    def close(self) -> None:
        self._release()

    def open_disk_writer(self, disk: TargetDiskRef, *, resume: bool = False, incremental: bool = False) -> DiskWriter:
        if disk.data.get("virtual_clone"):
            raise ProviderError("nutanix: a deploy clone is not written to")
        self.ctx.require_execute("nutanix write disk over iSCSI", disk=disk.name, incremental=incremental)
        return BlockDeviceWriter(self._device(disk), disk.size_bytes,
                                 prezeroed=not incremental and bool(self.config.get("assume_zeroed", True)))

    def open_disk_reader(self, disk: TargetDiskRef) -> DiskReader | None:
        if not self.config.get("verify_after_commit", True) or disk.data.get("virtual_clone"):
            return None
        return RawFileReader(self._device(disk), size=disk.size_bytes)

    def morph_image(self, disk: TargetDiskRef) -> tuple[Path, str] | None:
        if disk.data.get("virtual_clone"):
            return None           # deploys reuse the replica as synced; OSMorph ran when it was migrated
        return Path(self._device(disk)), "raw"

    # ------------------------------------------------------------------ replica
    def supports_replica(self) -> bool:
        return True

    def clone_disk(self, disk: TargetDiskRef, name: str, restore_point: str | None = None) -> TargetDiskRef:
        """Deploy: the VM is created with vm_disk_clone from the replica's volume-group disk, so the
        'clone' is a reference here and the copy happens (copy-on-write) when the VM is created."""
        if restore_point:
            raise ProviderError("nutanix: restore points are not supported; deploy from the latest sync")
        ensure_ours(self.api.volume_group(disk.data["vg"]), disk.data["vg"])
        self._release(disk.data["vg"])
        return TargetDiskRef(id=f"{disk.id}#{name}", name=name, size_bytes=disk.size_bytes,
                             data={**disk.data, "virtual_clone": True})

    def delete_disk(self, disk: TargetDiskRef) -> None:
        if disk.data.get("virtual_clone"):
            return
        vg = disk.data["vg"]
        info = self.api.volume_group(vg)
        if info is None:
            return
        ensure_ours(info, vg)
        self.ctx.require_execute("nutanix delete volume group", volume_group=disk.data.get("vg_name"))
        self._release(vg)
        self.api.delete_volume_group(vg)

    # ------------------------------------------------------------------ VM
    def target_bus(self, spec: VmSpec) -> DiskBus:
        return DiskBus.SCSI

    def _ours(self, vm: Mapping[str, Any] | None) -> bool:
        return bool(vm) and MARK in str(vm.get("description") or "")

    def create_vm(self, spec: VmSpec, boot_disk: TargetDiskRef, data_disks: list[TargetDiskRef]) -> VmRef:
        disks = [boot_disk, *data_disks]
        self._release()                       # LUNs logged out + initiator detached before cloning
        o = self._opts(spec)
        nets = [self._network(n.network, o) for n in spec.nics]
        macs = [n.mac if o.get("preserve_mac") else None for n in spec.nics]
        body = vm_body(spec, disks, nets, macs=macs)
        self.ctx.require_execute("nutanix create VM (clone disks from volume groups)", name=spec.name, disks=len(disks))
        uuid = self.api.create_vm(body)
        if o.get("power_on", True):
            self.ctx.require_execute("nutanix power on VM", vm=spec.name)
            self.api.power(uuid, "ON")
        if not o.get("keep_volume_groups", False):
            for d in disks:
                if not d.data.get("virtual_clone"):
                    try:
                        self.delete_disk(d)   # migration: the VM has its own (cloned) disks now
                    except Exception as e:  # noqa: BLE001 - the VM is fine; report the leftover
                        self.ctx.logger.warning("nutanix: volume group %s left behind: %s", d.data.get("vg_name"), e)
        return VmRef(uuid, spec.name, self.name)

    def find_vm(self, name: str) -> VmRef | None:
        hits = self.api.vms_by_name(name)
        if len(hits) > 1:
            raise SafetyGateError(f"nutanix: several VMs named {name!r}")
        return VmRef(str(hits[0]["uuid"]), name, self.name) if hits else None

    def attach_disk(self, vm: VmRef, disk: TargetDiskRef, order: int) -> None:
        if not self._ours(self.api.vm(vm.id)):
            raise SafetyGateError(f"refusing to change VM {vm.name}: not created by vmigrate")
        self._release(disk.data["vg"])
        self.ctx.require_execute("nutanix attach disk (clone)", vm=vm.name, disk=disk.name, order=order)
        self.api.attach_disk(vm.id, {"is_cdrom": False, "disk_address": {"device_bus": "scsi", "device_index": order},
                                     "vm_disk_clone": {"disk_address": {"vmdisk_uuid": disk.data["disk_ext"]},
                                                       "minimum_size": disk.size_bytes}})

    def list_attached_disks(self, vm: VmRef) -> list[str]:
        info = self.api.vm(vm.id)
        return attached_refs(info) if info else []

    def check_boot(self, vm: VmRef, timeout: float = 900, *, guest_os: str | None = None,
                   progress: Any = None) -> BootCheck:
        """Powered on and staying on for `boot_grace` seconds (default 180) passes, with a note to confirm in
        the Prism console (AHV exposes no serial console log through the API)."""
        say = progress or self.ctx.logger.info
        grace = float(self.config.get("boot_grace", 180))
        start, last = time.monotonic(), -1e9
        while time.monotonic() - start < timeout:
            info = self.api.vm(vm.id) or {}
            st = str(info.get("power_state", "")).lower()
            el = time.monotonic() - start
            if st == "off" and el > 30:
                return BootCheck(False, "VM is powered off (guest shut down or crashed)")
            if st == "on" and el >= grace:
                return BootCheck(True, f"powered on for {int(el)}s with no errors; confirm the login screen in the "
                                       "Prism console")
            if el - last >= 60:
                last = el
                say(f"boot check: VM {st or '?'}, {int(el)}s of {int(timeout)}s (passes after {int(grace)}s on)")
            cancel.sleep(self.poll)
        return BootCheck(False, f"VM not confirmed running within {int(timeout)}s")

    # ------------------------------------------------------------------ cleanup
    def cleanup_on_failure(self, created: Mapping[str, Any]) -> None:
        self.ctx.require_execute("nutanix cleanup created resources")
        self._release()
        vm = created.get("target_vm")
        if vm:
            info = self.api.vm(vm["id"])
            if info is None:
                self.ctx.logger.info("cleanup: VM %s is already gone", vm["id"])
            elif not self._ours(info):
                raise SafetyGateError(f"refusing to delete VM {vm['id']}: not created by vmigrate")
            else:
                if str(info.get("power_state", "")).lower() == "on":
                    self.api.power(vm["id"], "OFF")
                self.api.delete_vm(vm["id"])
        for d in created.get("target_disks", []):
            data = d.get("data") or {}
            if data.get("virtual_clone") or not data.get("vg"):
                continue
            self.delete_disk(TargetDiskRef(d["id"], d["name"], d["size_bytes"], data=data))
