"""Purpose: VMware vSphere TargetProvider: a powered-off holder VM gets thin disks on the target datastore,
each disk is written through nbdkit's VDDK plugin in write mode (NBD), and the holder is then turned into
the migrated VM and powered on.

Usage (endpoint config; the same endpoint can be a source too. Password is a credential):
    type: vmware
    host: vcenter.example.com        # vCenter (recommended) or ESXi
    username / password / verify_tls / thumbprint
    vddk_libdir: /opt/vmware-vix-disklib-distrib     # VDDK on this worker (as for the source)
    vddk_transports: nbdssl:nbd
    target_datacenter: DC1
    target_cluster: Prod             # or target_host: esx1.example.com
    target_resource_pool: null       # optional
    target_folder: null              # optional VM folder name
    target_datastore: ds-ssd-01
    scsi_controller: lsilogic-sas    # lsilogic-sas (default: Windows has the driver), pvscsi, lsilogic
    network_map: {"VLAN-120": "PG-120"}   # source network -> port group (standard or distributed)
    default_network: VM Network
    nic_adapter: auto                # auto: e1000e for Windows (inbox driver), vmxnet3 for Linux
    preserve_mac: false              # only MACs in VMware's 00:50:56 range can be kept
    power_on: true

How it works:
  holder   : the first disk of a run creates an empty, powered-off VM `vmigrate-holder-<run>` (extraConfig
             vmigrate.managed=true, vmigrate.holder=<run>) with the SCSI controller; disks are added to it
             in source order (15 per controller, up to 4 controllers)
  write    : nbdkit vddk (vm=moref, file=[ds] path.vmdk, no snapshot) on a private Unix socket; the copier
             writes over NBD; new thin disks read as zeros, so zero chunks are skipped
  OSMorph  : guestfish / hivexregedit open the same export (osmorph/vmware.py: Linux initramfs drivers,
             Windows LSI_SAS boot-start)
  VM       : the holder is renamed and reconfigured (CPU, memory, guest OS, EFI/BIOS + secure boot, boot
             disk, NICs on the mapped port groups) and powered on
  replica  : the holder of the first sync keeps the replica disks; syncs rewrite them over VDDK; a deploy
             copies them (CopyVirtualDisk, thin) into a new holder
Safety: only VMs with extraConfig vmigrate.managed=true are changed or deleted.
Needs: nbdkit + VDDK on the worker. Tested against fakes only.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...core import cancel
from ...core.context import RunContext
from ...core.errors import ConfigError, ProviderError, SafetyGateError
from ...core.interfaces import BootCheck, DiskHints, DiskReader, DiskWriter, TargetDiskRef, TargetProvider, VmRef
from ...ir import DiskBus, Firmware, OsFamily, VmSpec
from ..vhi.nbd import NbdDiskReader, NbdDiskWriter
from .target_client import GUEST_IDS
from .transports import VddkWriteExport, vddk_problems

MANAGED, HOLDER, DISKNAME = "vmigrate.managed", "vmigrate.holder", "vmigrate.disk."


def guest_id(spec: VmSpec, override: str | None = None) -> str:
    if override:
        return override
    if spec.guest_os.family == OsFamily.WINDOWS:
        return GUEST_IDS["windows"]
    if spec.guest_os.family == OsFamily.LINUX:
        return GUEST_IDS.get(str(spec.guest_os.distro or ""), GUEST_IDS["linux"])
    return "otherGuest64"


def nic_adapter(spec: VmSpec, setting: str = "auto") -> str:
    if setting in ("vmxnet3", "e1000e"):
        return setting
    return "e1000e" if spec.guest_os.family == OsFamily.WINDOWS else "vmxnet3"


def vm_settings(spec: VmSpec, o: Mapping[str, Any], boot_key: int, log: Any = None) -> dict[str, Any]:
    nets = []
    for nic in spec.nics:
        net = (o.get("network_map") or {}).get(nic.network) or o.get("default_network")
        if not net:
            raise ConfigError(f"vmware: no network_map entry for source network {nic.network!r} (and no default_network)")
        mac = None
        if o.get("preserve_mac") and nic.mac:
            if nic.mac.lower().startswith("00:50:56"):
                mac = nic.mac
            elif log:
                log.warning("vmware: MAC %s is outside VMware's 00:50:56 range; vSphere assigns a new one", nic.mac)
        nets.append({"network": net, "adapter": nic_adapter(spec, str(o.get("nic_adapter", "auto"))), "mac": mac})
    return {"name": spec.name, "num_cpu": spec.cpu.total, "cores_per_socket": spec.cpu.cores,
            "memory_mb": max(256, spec.memory_bytes >> 20), "guest_id": guest_id(spec, o.get("guest_id")),
            "firmware": "efi" if spec.firmware == Firmware.UEFI else "bios", "secure_boot": spec.secure_boot,
            "boot_disk_key": boot_key, "annotation": f"migrated by vmigrate from {spec.source_id}",
            "extra": {MANAGED: "true", HOLDER: ""}, "nics": nets}


class VmwareTarget(TargetProvider):
    type_name = "vmware"
    platform_family = "vmware"

    def __init__(self, name: str, config: Mapping[str, Any], ctx: RunContext, client: Any = None,
                 export_factory: Any = None):
        super().__init__(name, config, ctx)
        self._client = client
        self._export_factory = export_factory
        self._holder: str | None = None
        self._lock = threading.Lock()
        self._exports: dict[str, Any] = {}
        self.poll = float(config.get("poll_interval", 5))
        self.controller = str(config.get("scsi_controller", "lsilogic-sas"))
        if self.controller not in ("lsilogic-sas", "pvscsi", "lsilogic"):
            raise ConfigError("vmware target: scsi_controller must be lsilogic-sas, pvscsi or lsilogic")

    # ------------------------------------------------------------------ plumbing
    @property
    def client(self) -> Any:
        if self._client is None:
            from .target_client import VsphereTargetClient
            c = self.config
            self._client = VsphereTargetClient(
                c["host"], c["username"], c.get("password", ""), port=int(c.get("port", 443)),
                verify_tls=bool(c.get("verify_tls", True)), datacenter=c.get("target_datacenter"),
                cluster=c.get("target_cluster"), esx_host=c.get("target_host"),
                resource_pool=c.get("target_resource_pool"), folder=c.get("target_folder"),
                datastore=c.get("target_datastore"))
        return self._client

    def _ours(self, moref: str) -> dict[str, Any] | None:
        info = self.client.vm_info(moref)
        if info is None:
            return None
        if info["extra"].get(MANAGED) != "true":
            raise SafetyGateError(f"refusing to change VM {info['name']} ({moref}): not created by vmigrate")
        return info

    def _export(self, disk: TargetDiskRef) -> Any:
        key = disk.id
        if key not in self._exports:
            kw = dict(nbdkit=self.config.get("nbdkit", "nbdkit"), libdir=self.config.get("vddk_libdir", ""),
                      server=self.config["host"], user=self.config["username"], password=self.config.get("password", ""),
                      thumbprint=self.config.get("thumbprint") or self.client.thumbprint(),
                      vm_moref=disk.data["vm"], file_name=disk.data["file"],
                      transports=self.config.get("vddk_transports", "nbdssl:nbd"))
            self._exports[key] = (self._export_factory or VddkWriteExport)(**kw)
        return self._exports[key]

    def _close_exports(self, key: str | None = None) -> None:
        for k in [k for k in list(self._exports) if key is None or k == key]:
            try:
                self._exports.pop(k).close()
            except Exception as e:  # noqa: BLE001
                self.ctx.logger.warning("vmware: closing VDDK export %s: %s", k, e)

    def close(self) -> None:
        self._close_exports()

    # ------------------------------------------------------------------ checks + lists
    def check_connection(self) -> list[str]:
        try:
            self.client.about()
        except Exception as e:  # noqa: BLE001
            return [f"cannot connect to vSphere {self.config.get('host')}: {e}"]
        return self.client.check()

    def preflight(self, spec: VmSpec | None = None) -> list[str]:
        p = self.check_connection()
        if self._export_factory is None:              # tests inject their own export
            p += vddk_problems(self.config.get("nbdkit", "nbdkit"), self.config.get("vddk_libdir"))
        if spec is None or p:
            return p
        o = self._opts(spec)
        names = {n["name"] for n in self.client.networks()}
        for nic in spec.nics:
            net = (o.get("network_map") or {}).get(nic.network) or o.get("default_network")
            if not net:
                p.append(f"no network_map entry for source network {nic.network!r} (and no default_network)")
            elif net not in names:
                p.append(f"port group {net!r} (for {nic.network!r}) not found")
        ds = next((d for d in self.client.datastores() if d["name"] == self.config.get("target_datastore")), None)
        need = sum(d.size_bytes for d in spec.disks)
        if ds and ds["free"] < need:
            self.ctx.logger.warning("vmware: datastore %s has %d GiB free, the disks are %d GiB provisioned (thin: only "
                                    "written data is used)", ds["name"], ds["free"] >> 30, need >> 30)
        return p

    def _opts(self, spec: VmSpec) -> dict[str, Any]:
        t = spec.extra.get("target_options") or {}
        return {**self.config, **t, "network_map": {**(self.config.get("network_map") or {}), **(t.get("network_map") or {})}}

    def list_networks(self) -> list[dict[str, Any]]:
        return sorted(({"id": n["name"], "name": n["name"]} for n in self.client.networks()), key=lambda n: n["id"])

    def list_storage(self) -> list[dict[str, Any]]:
        return sorted(({"id": d["name"], "name": f"{d['name']} ({d['free'] >> 30} GiB free)"}
                       for d in self.client.datastores() if d.get("accessible", True)), key=lambda d: d["id"])

    # ------------------------------------------------------------------ holder + disks
    def _holder_tag(self) -> str:
        return (self.ctx.run_id or "run")[:12]

    def _ensure_holder(self) -> str:
        with self._lock:
            if self._holder:
                return self._holder
            tag = self._holder_tag()
            hit = [v for v in self.client.managed_vms() if v["extra"].get(HOLDER) == tag]
            if hit:
                self._holder = hit[0]["moref"]
                return self._holder
            name = f"vmigrate-holder-{tag}"
            self.ctx.require_execute("vmware create holder VM", name=name, controller=self.controller)
            self._holder = self.client.create_vm(
                name, controller=self.controller,
                annotation="vmigrate: holds the disks of a migration in progress; becomes the migrated VM",
                extra={MANAGED: "true", HOLDER: tag})
            return self._holder

    def _ref(self, moref: str, d: Mapping[str, Any], name: str) -> TargetDiskRef:
        return TargetDiskRef(id=d["uuid"], name=name, size_bytes=int(d["capacity"]),
                             data={"vm": moref, "key": d["key"], "file": d["file"]})

    def create_disk(self, name: str, size_bytes: int, hints: DiskHints) -> TargetDiskRef:
        if hints.shared:
            raise ConfigError("vmware target: shared (multi-writer) cluster disks are not supported yet")
        moref = self._ensure_holder()
        size = -(-size_bytes // (1 << 20)) * (1 << 20)
        index = len((self.client.vm_info(moref) or {}).get("disks", []))
        self.ctx.require_execute("vmware add thin disk to holder", vm=moref, name=name, size_bytes=size)
        d = self.client.add_disk(moref, size, index, controller=self.controller)
        self._remember(moref, d["key"], name)
        return self._ref(moref, d, name)

    def _remember(self, moref: str, key: int, name: str) -> None:
        """The disk's name goes into the holder's extraConfig, so find_disk works after a restart."""
        self._ours(moref)
        self.client.set_extra(moref, {f"{DISKNAME}{key}": name})

    def find_disk(self, name: str) -> TargetDiskRef | None:
        hits = []
        for v in self.client.managed_vms():
            for k, val in v["extra"].items():
                if k.startswith(DISKNAME) and val == name:
                    info = self.client.vm_info(v["moref"]) or {}
                    d = next((x for x in info.get("disks", []) if str(x["key"]) == k[len(DISKNAME):]), None)
                    if d:
                        hits.append(self._ref(v["moref"], d, name))
        if len(hits) > 1:
            raise SafetyGateError(f"several vmigrate disks named {name!r}")
        return hits[0] if hits else None

    # ------------------------------------------------------------------ data path
    def open_disk_writer(self, disk: TargetDiskRef, *, resume: bool = False, incremental: bool = False) -> DiskWriter:
        info = self._ours(disk.data["vm"])
        if info and info["power"] == "poweredOn":
            raise SafetyGateError(f"vmware: {info['name']} is powered on; VDDK writes need it off")
        self.ctx.require_execute("vmware write disk through VDDK", disk=disk.name, file=disk.data["file"],
                                 incremental=incremental)
        return NbdDiskWriter(self._export(disk).client(), disk.size_bytes, prezeroed=not incremental)

    def open_disk_reader(self, disk: TargetDiskRef) -> DiskReader | None:
        if not self.config.get("verify_after_commit", True):
            return None
        return NbdDiskReader(self._export(disk).client(), disk.size_bytes)

    def morph_image(self, disk: TargetDiskRef) -> tuple[Path, str] | None:
        return (self._export(disk).uri, "raw")  # type: ignore[return-value]

    # ------------------------------------------------------------------ replica
    def supports_replica(self) -> bool:
        return True

    def clone_disk(self, disk: TargetDiskRef, name: str, restore_point: str | None = None) -> TargetDiskRef:
        if restore_point:
            raise ProviderError("vmware: restore points are not supported; deploy from the latest sync")
        self._ours(disk.data["vm"])
        self._close_exports(disk.id)
        holder = self._ensure_holder()
        info = self.client.vm_info(holder) or {}
        dst = f"{info['vmx_dir']}vmigrate-{name}.vmdk"
        self.ctx.require_execute("vmware copy disk (thin)", source=disk.data["file"], target=dst)
        self.client.copy_disk(disk.data["file"], dst)
        d = self.client.add_disk(holder, disk.size_bytes, len(info.get("disks", [])), controller=self.controller,
                                 existing_file=dst)
        self._remember(holder, d["key"], name)
        return self._ref(holder, d, name)

    def delete_disk(self, disk: TargetDiskRef) -> None:
        info = self._ours(disk.data["vm"])
        if info is None:
            return
        self.ctx.require_execute("vmware delete disk", vm=info["name"], file=disk.data["file"])
        self._close_exports(disk.id)
        self.client.remove_disk(disk.data["vm"], int(disk.data["key"]), destroy=True)
        left = (self.client.vm_info(disk.data["vm"]) or {}).get("disks", [])
        if not left and info["extra"].get(HOLDER):
            self.ctx.logger.info("vmware: removing empty holder VM %s", info["name"])
            self.client.destroy(disk.data["vm"])

    # ------------------------------------------------------------------ VM
    def target_bus(self, spec: VmSpec) -> DiskBus:
        return DiskBus.SCSI

    def morph_options(self) -> dict[str, Any]:
        return {"vmware_controller": self.controller}

    def create_vm(self, spec: VmSpec, boot_disk: TargetDiskRef, data_disks: list[TargetDiskRef]) -> VmRef:
        disks = [boot_disk, *data_disks]
        owners = {d.data["vm"] for d in disks}
        if len(owners) != 1:
            raise ProviderError(f"vmware: the disks belong to several VMs {sorted(owners)}; cannot build one VM")
        moref = owners.pop()
        info = self._ours(moref)
        if info is None:
            raise ProviderError(f"vmware: holder VM {moref} is gone")
        self._close_exports()                         # VDDK sessions end before the VM is changed and started
        o = {**self._opts(spec), "scsi_controller": self.controller}
        cfg = vm_settings(spec, o, int(boot_disk.data["key"]), self.ctx.logger)
        self.ctx.require_execute("vmware configure VM", vm=moref, name=spec.name, disks=len(disks))
        self.client.reconfigure(moref, cfg)
        if o.get("power_on", True):
            self.ctx.require_execute("vmware power on VM", vm=spec.name)
            self.client.power_on(moref)
        return VmRef(moref, spec.name, self.name)

    def find_vm(self, name: str) -> VmRef | None:
        hits = self.client.find_vms(name)
        if len(hits) > 1:
            raise SafetyGateError(f"vmware: several VMs named {name!r}")
        return VmRef(hits[0]["moref"], name, self.name) if hits else None

    def attach_disk(self, vm: VmRef, disk: TargetDiskRef, order: int) -> None:
        if disk.data.get("vm") == vm.id:
            return                                    # created on this VM already
        raise ProviderError(f"vmware: disk {disk.name} belongs to another VM ({disk.data.get('vm')})")

    def list_attached_disks(self, vm: VmRef) -> list[str]:
        return [d["uuid"] for d in (self.client.vm_info(vm.id) or {}).get("disks", [])]

    def check_boot(self, vm: VmRef, timeout: float = 900, *, guest_os: str | None = None,
                   progress: Any = None) -> BootCheck:
        """VMware Tools running = booted. Without tools (not installed yet: OSMorph schedules them for the first
        boot) the VM passes after `boot_grace` seconds powered on (default 240), with a note to check the console."""
        say = progress or self.ctx.logger.info
        grace = float(self.config.get("boot_grace", 240))
        start, last = time.monotonic(), -1e9
        while time.monotonic() - start < timeout:
            info = self.client.vm_info(vm.id) or {}
            el = time.monotonic() - start
            if info.get("power") == "poweredOff" and el > 30:
                return BootCheck(False, "VM powered off during the boot check (guest shut down or crashed)")
            if info.get("tools") == "guestToolsRunning":
                return BootCheck(True, "VMware Tools are running in the guest")
            if info.get("power") == "poweredOn" and el >= grace:
                return BootCheck(True, f"powered on for {int(el)}s; VMware Tools not running yet. Confirm the login "
                                       "screen in the vSphere console")
            if el - last >= 60:
                last = el
                say(f"boot check: {info.get('power', '?')}, tools {info.get('tools') or 'not running'}, "
                    f"{int(el)}s of {int(timeout)}s")
            cancel.sleep(self.poll)
        return BootCheck(False, f"VM not confirmed booted within {int(timeout)}s")

    # ------------------------------------------------------------------ cleanup
    def cleanup_on_failure(self, created: Mapping[str, Any]) -> None:
        self.ctx.require_execute("vmware cleanup created resources")
        self._close_exports()
        vms = set()
        if created.get("target_vm"):
            vms.add(created["target_vm"]["id"])
        for d in created.get("target_disks", []):
            if (d.get("data") or {}).get("vm"):
                vms.add(d["data"]["vm"])
        for moref in sorted(vms):
            info = self._ours(moref)
            if info is None:
                self.ctx.logger.info("cleanup: VM %s is already gone", moref)
                continue
            self.client.power_off(moref)
            self.client.destroy(moref)                # its disks go with it
