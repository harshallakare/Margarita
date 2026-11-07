"""Purpose: Virtuozzo Hybrid Infrastructure (VHI) TargetProvider — the reference vertical's target.
Must run ON a VHI node (vStorage mounted, root) because it writes volume backings directly.

Usage (config):
    targets:
      vhi1:
        type: vhi
        auth_url: https://vhi.example:5000/v3
        username: admin
        password: ${VHI_PASSWORD}
        user_domain_name: Default
        project_id: 0123abcd...               # scope by ID (pitfall 7)
        verify_tls: false
        volume_type: vmds                     # VMDS-backed Cinder volume type
        vstorage_mount: /mnt/vstorage
        cinder_datastore: vols/datastores/cinder   # default
        staging_dir: /var/lib/vmigrate/staging
        network_map: {"VLAN-120": "<neutron net uuid>"}
        default_flavor: null                  # else auto-pick smallest fitting flavor
        preserve_mac: false                   # create Neutron ports with the source MAC/IP
        boot_bus: scsi                        # hw_disk_bus on the boot volume (pitfall 6)
        verify_after_commit: true             # sampled read-back of the qcow2 via qemu-img dd
    migrations[].target_options: {flavor, network_map, availability_zone}

Disk path (proven by hand):
  create Cinder VMDS volume (size rounded UP to GiB) -> wait available (detached)
  -> stage raw locally (sparse, zero-skipped) -> qemu-img convert -W -m 16 -O qcow2
     -o compat=1.1,cluster_size=1M  INTO  <vstorage>/vols/datastores/cinder/volume-<ID>/volume-<ID>
     (in place; .info/.qemu_img_info sidecars untouched; format stays qcow2; size == volume size)
  -> chown root:vstorage-users, chmod 660 -> header check virtual size == volume size
VM: boot volume gets hw_disk_bus=scsi + hw_scsi_model=virtio-scsi (+ UEFI props), Nova
create-from-volume with ALL volumes in block_device_mapping_v2 (boot_index 0, data -1 in order)
so the guest sees every disk on first boot. NOT Glance (multi-TB uploads reset, pitfall 8).
"""

from __future__ import annotations

import os
import re
import shutil
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...core.context import RunContext
from ...core.errors import ConfigError, ProviderError, SafetyGateError
from ...core.fileio import RawFileWriter
from ...core.interfaces import (
    BootCheck,
    DiskHints,
    DiskReader,
    DiskWriter,
    TargetDiskRef,
    TargetProvider,
    VmRef,
)
from ...core.retry import poll_until
from ...ir import DiskBus, Firmware, VmSpec
from .qemu import (
    QemuImageReader,
    QemuImg,
    chown_vstorage,
    convert_argv,
    parse_qcow2_header,
    to_raw_argv,
)

GIB = 1 << 30
MANAGED = "vmigrate:managed"
BOOT_FAIL_PATTERNS = [
    r"emergency mode", r"Entering emergency", r"dracut-initqueue.*timeout", r"Kernel panic",
    r"Give root password for maintenance", r"grub rescue>", r"No bootable device", r"VFS: Unable to mount root",
]
BOOT_OK_PATTERNS = [r"login:\s*$", r"\blogin: ", r"Reached target .*Multi-User", r"Cloud-init .* finished"]


def round_up_gib(n: int) -> int:
    return max(1, -(-n // GIB))


def backing_path(vstorage_mount: str | Path, volume_id: str, datastore: str = "vols/datastores/cinder") -> Path:
    return Path(vstorage_mount) / datastore / f"volume-{volume_id}" / f"volume-{volume_id}"


def boot_volume_metadata(spec: VmSpec, bus: DiskBus) -> dict[str, str]:
    md = {"hw_disk_bus": bus.value}
    if bus == DiskBus.SCSI:
        md["hw_scsi_model"] = "virtio-scsi"
    if spec.firmware == Firmware.UEFI:
        md["hw_firmware_type"] = "uefi"
        md["hw_machine_type"] = "q35"
        if spec.secure_boot:
            md["os_secure_boot"] = "required"
    return md


def pick_flavor(flavors: list[dict[str, Any]], vcpus: int, ram_mib: int) -> dict[str, Any] | None:
    fits = [f for f in flavors if f.get("vcpus", 0) >= vcpus and f.get("ram", 0) >= ram_mib]
    fits.sort(key=lambda f: (f.get("vcpus", 0), f.get("ram", 0), f.get("disk", 0)))
    return fits[0] if fits else None


def _dev_key(dev: str) -> tuple[int, str]:
    m = re.match(r"/dev/[a-z]+?d([a-z]+)$", dev or "")
    s = m.group(1) if m else (dev or "")
    return (len(s), s)  # sda < sdz < sdaa


class VhiTarget(TargetProvider):
    type_name = "vhi"
    platform_family = "kvm"

    def __init__(self, name: str, config: Mapping[str, Any], ctx: RunContext, api: Any = None,
                 qemu: QemuImg | None = None):
        super().__init__(name, config, ctx)
        self._api = api
        self.qemu = qemu or QemuImg(config.get("qemu_img", "qemu-img"))
        self.vstorage = Path(config.get("vstorage_mount", "/mnt/vstorage"))
        self.datastore = config.get("cinder_datastore", "vols/datastores/cinder")
        self.staging = Path(config.get("staging_dir", "/var/lib/vmigrate/staging"))
        self.volume_type = config.get("volume_type")
        self.group = config.get("vstorage_group", "vstorage-users")
        self.poll = float(config.get("poll_interval", 5))
        self.op_timeout = float(config.get("op_timeout", 1800))
        self.set_owner = config.get("chown_backing", True)
        if not self.volume_type:
            raise ConfigError(f"targets.{name}: volume_type (the VMDS volume type) is required")

    @property
    def api(self) -> Any:
        if self._api is None:
            from .api import OpenStackApi
            from .auth import connect

            self._api = OpenStackApi(connect(self.config))
        return self._api

    def _wait_volume(self, vol_id: str, status: str) -> dict[str, Any]:
        return poll_until(lambda: self.api.get_volume(vol_id), lambda v: v.get("status") == status,
                          failed=lambda v: str(v.get("status", "")).startswith("error"),
                          timeout=self.op_timeout, interval=self.poll, what=f"volume {vol_id} -> {status}")

    # ------------------------------------------------------------------ preflight (read-only)
    def _opts(self, spec: VmSpec) -> dict[str, Any]:
        return {**self.config, **(spec.extra.get("target_options") or {}),
                "network_map": {**(self.config.get("network_map") or {}),
                                **((spec.extra.get("target_options") or {}).get("network_map") or {})}}

    def preflight(self, spec: VmSpec | None = None) -> list[str]:
        p: list[str] = []
        try:
            pid = self.api.current_project_id()
            if pid != self.config.get("project_id"):
                p.append(f"token scoped to project {pid}, expected {self.config.get('project_id')} "
                         "(clear OS_PROJECT_* env vars; grant the user a role on the target project)")
        except Exception as e:  # noqa: BLE001 - report, don't crash preflight
            p.append(f"cannot authenticate to VHI/Keystone: {e}")
            return p
        if not self.api.find_volume_type(self.volume_type):
            p.append(f"volume type {self.volume_type!r} not found")
        ds = self.vstorage / self.datastore
        if not ds.is_dir():
            p.append(f"vStorage cinder datastore not found at {ds} (run on a VHI node with vStorage mounted)")
        if shutil.which(self.qemu.binary) is None:
            p.append(f"{self.qemu.binary} not found on PATH")
        if spec is None:
            return p
        o = self._opts(spec)
        for nic in spec.nics:
            tgt = o["network_map"].get(nic.network)
            if not tgt:
                p.append(f"no network_map entry for source network {nic.network!r}")
            elif not self.api.find_network(tgt):
                p.append(f"target network {tgt!r} (for {nic.network!r}) not found")
        if o.get("flavor") or o.get("default_flavor"):
            if not self.api.find_flavor(o.get("flavor") or o.get("default_flavor")):
                p.append(f"flavor {o.get('flavor') or o.get('default_flavor')!r} not found")
        elif not pick_flavor(self.api.flavors(), spec.cpu.total, spec.memory_bytes >> 20):
            p.append(f"no flavor with >= {spec.cpu.total} vCPU and >= {spec.memory_bytes >> 20} MiB RAM")
        # Pitfall 10: staging must hold the largest N disks in flight at once.
        par = int(spec.extra.get("max_parallel_disks", 1))
        need = sum(sorted((round_up_gib(d.size_bytes) * GIB for d in spec.disks), reverse=True)[:par])
        probe = self.staging if self.staging.exists() else self.staging.parent
        if probe.exists():
            free = shutil.disk_usage(probe).free
            if free < need:
                p.append(f"staging {self.staging} has {free / GIB:.0f} GiB free; {need / GIB:.0f} GiB needed for "
                         f"{par} disks in flight (lower engine.max_parallel_disks or add space)")
        else:
            p.append(f"staging dir parent {probe} does not exist")
        return p

    # ------------------------------------------------------------------ disks
    def create_disk(self, name: str, size_bytes: int, hints: DiskHints) -> TargetDiskRef:
        gib = round_up_gib(size_bytes)
        self.ctx.require_execute("cinder create volume", name=name, size_gib=gib, type=self.volume_type)
        vol = self.api.create_volume(name, gib, self.volume_type,
                                     {MANAGED: "true", "vmigrate:run": self.ctx.run_id,
                                      "vmigrate:order": str(hints.order), "vmigrate:boot": str(hints.is_boot).lower()},
                                     availability_zone=self.config.get("availability_zone"))
        vol = self._wait_volume(vol["id"], "available")
        return TargetDiskRef(id=vol["id"], name=name, size_bytes=int(vol["size"]) * GIB, data={"size_gib": int(vol["size"])})

    def find_disk(self, name: str) -> TargetDiskRef | None:
        mine = [v for v in self.api.volumes_by_name(name) if (v.get("metadata") or {}).get(MANAGED) == "true"]
        if len(mine) > 1:
            raise SafetyGateError(f"multiple vmigrate volumes named {name!r}: {[v['id'] for v in mine]}")
        if not mine:
            return None
        v = mine[0]
        return TargetDiskRef(id=v["id"], name=name, size_bytes=int(v["size"]) * GIB, data={"size_gib": int(v["size"])})

    def _staged(self, disk: TargetDiskRef) -> Path:
        return self.staging / f"volume-{disk.id}.raw"

    def _backing(self, disk: TargetDiskRef) -> Path:
        return backing_path(self.vstorage, disk.id, self.datastore)

    def open_disk_writer(self, disk: TargetDiskRef, *, resume: bool = False, incremental: bool = False) -> DiskWriter:
        self.ctx.require_execute("stage raw", disk=disk.name, path=str(self._staged(disk)), incremental=incremental)
        p = self._staged(disk)
        if incremental:
            # Replica sync: the staging raw must equal the committed replica before deltas are applied.
            # Rebuild it from the VMDS backing unless a previous (interrupted) sync left it in place.
            if not p.exists():
                backing = self._backing(disk)
                if not backing.exists():
                    raise ProviderError(f"{disk.name}: replica backing missing at {backing}")
                p.parent.mkdir(parents=True, exist_ok=True)
                self.qemu.run(to_raw_argv(backing, p, qemu_img=self.qemu.binary))
            w = RawFileWriter(p, disk.size_bytes)
            w.prezeroed = False   # existing content: a changed region that became zero MUST be written
            return w
        if not resume and p.exists():
            p.unlink()
        # Sized to the Cinder volume (GiB-rounded) so the qcow2 virtual size matches exactly.
        return RawFileWriter(p, disk.size_bytes)

    # ------------------------------------------------------------------ replica support
    def supports_replica(self) -> bool:
        return True

    def supports_restore_points(self) -> tuple[bool, str]:
        # Cinder snapshots of VMDS volumes whose backing we rewrite in place are unverified (the
        # snapshot may share the qcow2 chain). Until proven on a VHI cluster, deploys clone the
        # latest consistent sync instead.
        return False, "VHI: restore points disabled (Cinder snapshot vs direct backing writes unverified)"

    def clone_disk(self, disk: TargetDiskRef, name: str, restore_point: str | None = None) -> TargetDiskRef:
        if restore_point:
            raise ProviderError("VHI: restore points are not supported; deploy from the latest sync")
        src = self.api.get_volume(disk.id)
        if (src.get("metadata") or {}).get(MANAGED) != "true":
            raise SafetyGateError(f"refusing to clone volume {disk.id}: not vmigrate-managed")
        if src.get("status") != "available" or src.get("attachments"):
            raise SafetyGateError(f"{disk.name}: replica volume must be available/detached to clone")
        gib = round_up_gib(disk.size_bytes)
        self.ctx.require_execute("cinder clone volume", source=disk.id, name=name, size_gib=gib)
        meta = {k: v for k, v in (src.get("metadata") or {}).items() if k.startswith("vmigrate:")}
        meta.update({MANAGED: "true", "vmigrate:run": self.ctx.run_id, "vmigrate:clone_of": disk.id})
        vol = self.api.create_volume(name, gib, self.volume_type, meta,
                                     availability_zone=self.config.get("availability_zone"), source_volid=disk.id)
        vol = self._wait_volume(vol["id"], "available")
        return TargetDiskRef(id=vol["id"], name=name, size_bytes=int(vol["size"]) * GIB, data={"size_gib": int(vol["size"])})

    def delete_disk(self, disk: TargetDiskRef) -> None:
        v = self.api.get_volume(disk.id)
        if (v.get("metadata") or {}).get(MANAGED) != "true":
            raise SafetyGateError(f"refusing to delete volume {disk.id}: not vmigrate-managed")
        self.ctx.require_execute("cinder delete volume", id=disk.id, name=disk.name)
        self.api.delete_volume(disk.id)
        self._staged(disk).unlink(missing_ok=True)

    def commit_disk(self, disk: TargetDiskRef, writer_staged: Path | None) -> None:
        if writer_staged is None or not writer_staged.exists():
            raise ProviderError(f"{disk.name}: staged image missing ({writer_staged})")
        vol = self.api.get_volume(disk.id)
        if vol.get("status") != "available" or vol.get("attachments"):
            raise SafetyGateError(f"{disk.name}: volume must be detached/available to write its backing "
                                  f"(status={vol.get('status')}, attachments={len(vol.get('attachments') or [])})")
        backing = self._backing(disk)
        if not backing.exists():
            raise ProviderError(f"{disk.name}: VMDS backing not found at {backing}")
        with open(backing, "rb") as f:
            parse_qcow2_header(f.read(104))  # must already be qcow2 — we keep the format
        if os.path.getsize(writer_staged) != disk.size_bytes:
            raise SafetyGateError(f"{disk.name}: staged size {os.path.getsize(writer_staged)} != volume {disk.size_bytes}")
        self.ctx.require_execute("qemu-img convert into VMDS backing", disk=disk.name, backing=str(backing))
        t0 = time.monotonic()
        self.qemu.run(convert_argv(writer_staged, backing, qemu_img=self.qemu.binary,
                                   extra=self.config.get("convert_opts"),
                                   o=self.config.get("qcow2_opts", "compat=1.1,cluster_size=1048576")))
        self.ctx.logger.info("%s: qemu-img convert %.0fs", disk.name, time.monotonic() - t0)
        if self.set_owner:
            chown_vstorage(backing, self.group)
        with open(backing, "rb") as f:
            hdr = parse_qcow2_header(f.read(104))
        if hdr["virtual_size"] != disk.size_bytes:
            raise SafetyGateError(f"{disk.name}: qcow2 virtual size {hdr['virtual_size']} != volume {disk.size_bytes}")

    def open_disk_reader(self, disk: TargetDiskRef) -> DiskReader | None:
        if not self.config.get("verify_after_commit", True):
            return None
        return QemuImageReader(self._backing(disk), disk.size_bytes, self.qemu)

    def discard_staging(self, disk: TargetDiskRef) -> None:
        if not self.config.get("keep_staging", False):
            self._staged(disk).unlink(missing_ok=True)

    def morph_image(self, disk: TargetDiskRef) -> tuple[Path, str] | None:
        b = self._backing(disk)
        return (b, "qcow2") if b.exists() else None

    # ------------------------------------------------------------------ VM
    def target_bus(self, spec: VmSpec) -> DiskBus:
        return DiskBus(self._opts(spec).get("boot_bus", "scsi"))

    def prepare_boot_disk(self, disk: TargetDiskRef, spec: VmSpec, bus: DiskBus) -> None:
        md = boot_volume_metadata(spec, bus)
        self.ctx.require_execute("cinder set boot volume image metadata", volume=disk.id, metadata=md)
        self.api.volume_action(disk.id, {"os-set_bootable": {"bootable": True}})
        self.api.volume_action(disk.id, {"os-set_image_metadata": {"metadata": md}})

    def _networks(self, spec: VmSpec) -> list[dict[str, str]]:
        o = self._opts(spec)
        nets = []
        for i, nic in enumerate(spec.nics):
            net = self.api.find_network(o["network_map"][nic.network])
            if o.get("preserve_mac") and nic.mac:
                kw: dict[str, Any] = {"network_id": net["id"], "mac_address": nic.mac,
                                      "name": f"vmigrate-{spec.name}-nic{i}"}
                if o.get("preserve_ip", True) and nic.ipv4:
                    kw["fixed_ips"] = [{"ip_address": nic.ipv4}]
                self.ctx.require_execute("neutron create port", **kw)
                nets.append({"port": self.api.create_port(**kw)["id"]})
            else:
                nets.append({"uuid": net["id"]})
        return nets

    def create_vm(self, spec: VmSpec, boot_disk: TargetDiskRef, data_disks: list[TargetDiskRef]) -> VmRef:
        o = self._opts(spec)
        fl = o.get("flavor") or o.get("default_flavor")
        flavor = self.api.find_flavor(fl) if fl else pick_flavor(self.api.flavors(), spec.cpu.total, spec.memory_bytes >> 20)
        if not flavor:
            raise ProviderError(f"no usable flavor for {spec.name}")
        bdm = [{"boot_index": 0, "uuid": boot_disk.id, "source_type": "volume",
                "destination_type": "volume", "delete_on_termination": False}]
        if o.get("attach_at_create", True):
            bdm += [{"boot_index": -1, "uuid": d.id, "source_type": "volume", "destination_type": "volume",
                     "delete_on_termination": False} for d in data_disks]
        self.ctx.require_execute("nova create server", name=spec.name, flavor=flavor["id"], volumes=len(bdm))
        kw: dict[str, Any] = {"name": spec.name, "flavor_id": flavor["id"], "networks": self._networks(spec),
                              "block_device_mapping": bdm,
                              "metadata": {MANAGED: "true", "vmigrate:run": self.ctx.run_id,
                                           "vmigrate:source_id": spec.source_id}}
        if o.get("availability_zone"):
            kw["availability_zone"] = o["availability_zone"]
        if o.get("security_groups"):
            kw["security_groups"] = [{"name": g} for g in o["security_groups"]]
        srv = self.api.create_server(**kw)
        srv = poll_until(lambda: self.api.get_server(srv["id"]), lambda s: s.get("status") == "ACTIVE",
                         failed=lambda s: s.get("status") == "ERROR", timeout=self.op_timeout,
                         interval=self.poll, what=f"server {spec.name} ACTIVE")
        return VmRef(srv["id"], spec.name, self.name)

    def find_vm(self, name: str) -> VmRef | None:
        s = self.api.find_server(name)
        return VmRef(s["id"], name, self.name) if s else None

    def attach_disk(self, vm: VmRef, disk: TargetDiskRef, order: int) -> None:
        self.ctx.require_execute("nova attach volume", vm=vm.name, volume=disk.id, order=order)
        self.api.attach_volume(vm.id, disk.id)
        self._wait_volume(disk.id, "in-use")

    def list_attached_disks(self, vm: VmRef) -> list[str]:
        atts = sorted(self.api.attachments(vm.id), key=lambda a: _dev_key(a.get("device", "")))
        return [a["volume_id"] for a in atts]

    def check_boot(self, vm: VmRef, timeout: float = 900) -> BootCheck:
        deadline = time.monotonic() + timeout
        log = ""
        while time.monotonic() < deadline:
            s = self.api.get_server(vm.id)
            if s.get("status") == "ERROR":
                return BootCheck(False, f"server in ERROR: {s.get('fault')}")
            log = self.api.console_log(vm.id, length=400) or ""
            for pat in BOOT_FAIL_PATTERNS:
                if re.search(pat, log, re.I | re.M):
                    return BootCheck(False, f"guest boot failure: /{pat}/ in console", log)
            for pat in BOOT_OK_PATTERNS:
                if re.search(pat, log, re.I | re.M):
                    return BootCheck(True, "login prompt reached", log)
            time.sleep(self.poll * 2)
        if not log.strip():
            return BootCheck(True, "server ACTIVE; no serial console output — verify boot manually", log)
        return BootCheck(False, f"no login prompt within {timeout}s", log)

    # ------------------------------------------------------------------ cleanup
    def cleanup_on_failure(self, created: Mapping[str, Any]) -> None:
        self.ctx.require_execute("vhi cleanup created resources")
        vm = created.get("target_vm")
        if vm:
            s = self.api.get_server(vm["id"])
            if (s.get("metadata") or {}).get(MANAGED) != "true":
                raise SafetyGateError(f"refusing to delete server {vm['id']}: not vmigrate-managed")
            self.api.delete_server(vm["id"])
            poll_until(lambda: self.api.find_server(vm["name"]), lambda x: x is None,
                       timeout=self.op_timeout, interval=self.poll, what="server delete")
            i = 0
            while ports := self.api.ports_by_name(f"vmigrate-{vm['name']}-nic{i}"):
                for p in ports:
                    self.api.delete_port(p["id"])
                i += 1
        for d in created.get("target_disks", []):
            v = self.api.get_volume(d["id"])
            if (v.get("metadata") or {}).get(MANAGED) != "true":
                raise SafetyGateError(f"refusing to delete volume {d['id']}: not vmigrate-managed")
            self.api.delete_volume(d["id"])
            self._staged(TargetDiskRef(d["id"], d["name"], d["size_bytes"])).unlink(missing_ok=True)
