"""Purpose: Proxmox VE TargetProvider: disks allocated on a PVE storage, written over SSH on the target
node through the same small NBD agent the VHI helper uses, then a qemu VM is configured around them.

Usage (endpoint config; the same endpoint can be a source too. Token secret + SSH key are credentials):
    type: proxmox
    url: https://pve1.example.com:8006
    token_id: migrate@pve!vmigrate       # VM.Allocate, VM.Config.*, VM.PowerMgmt, VM.Audit, Datastore.AllocateSpace,
                                         # Datastore.Audit, Sys.Audit, SDN.Use (PVE 8 bridges)
    token_secret / ssh_user / ssh_private_key / ssh_known_hosts / node_addresses   # as for the source
    target_node: pve1                    # node that runs the new VMs (default: first online node)
    target_storage: local-lvm            # images storage on that node (lvmthin, zfspool, rbd, dir, nfs, lvm ...)
    network_map: {"VLAN-120": "vmbr0"}   # source network -> bridge (or "vmbr0:120" = bridge + VLAN tag)
    default_bridge: vmbr0                # for unmapped networks (unset: unmapped networks are an error)
    cpu_type: x86-64-v2-AES              # optional (PVE default otherwise)
    preserve_mac: false
    power_on: true                       # start the VM after creating it (the boot check needs it running)

How it works:
  holder   : the first disk of a run creates an empty, stopped VM `vmigrate-holder-<run>` (tags vmigrate,
             vmigrate-holder): PVE volumes must belong to a VM ID (vm-<id>-...). create_vm turns that
             holder into the migrated VM (rename + configure), so no disk is ever reassigned.
  disk     : POST /nodes/<node>/storage/<storage>/content  -> volid vm-<id>-vmigrate-<name>[.raw]
  write    : `pvesm path <volid>` over SSH (Ceph RBD: `rbd map`); the NBD agent serves it on the SSH
             session's stdin/stdout; zero chunks are skipped on thin storages (lvmthin, zfs, rbd, files)
  OSMorph  : guestfish / virt-v2v on this worker open the same volume over NBD (Unix-socket bridge)
  VM       : scsihw virtio-scsi-pci, scsi0..N in source order, net virtio on the mapped bridges, UEFI ->
             OVMF + q35 + efidisk0 (+ tpmstate0 for Windows with secure boot), qemu agent on; started
  replica  : volumes are rewritten in place; deploy clones a volume with `qemu-img convert -n` on the node
Safety: only VMs tagged vmigrate are ever changed or deleted, only volumes owned by such a VM are deleted.
Tested against fakes only (fake PVE API + the real NBD agent on local files).
"""

from __future__ import annotations

import os
import re
import tempfile
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
from ..vhi.nbd import NbdClient, NbdDiskReader, NbdDiskWriter, UnixBridge
from ..vhi.nbd_agent import __file__ as _AGENT_FILE

TAG = "vmigrate"
HOLDER_TAG = "vmigrate-holder"
MIB = 1 << 20
THIN = {"lvmthin", "zfspool", "rbd", "dir", "nfs", "cifs", "glusterfs", "cephfs", "btrfs"}
FILE_BASED = {"dir", "nfs", "cifs", "glusterfs", "cephfs", "btrfs"}
SUPPORTED = THIN | {"lvm"}
DISK_KEY = re.compile(r"^(scsi|virtio|sata|ide)(\d+)$")
_BUS_ORDER = {"scsi": 0, "virtio": 1, "sata": 2, "ide": 3}
_AGENT_SRC = Path(_AGENT_FILE).read_text()


# ------------------------------------------------------------------ pure helpers (unit-tested)
def tags_of(cfg: Mapping[str, Any]) -> set[str]:
    return {t for t in re.split(r"[;, ]+", str(cfg.get("tags") or "")) if t}


def volume_filename(vmid: int, name: str, storage_type: str) -> str:
    slug = re.sub(r"[^a-z0-9_.-]+", "-", name.lower()).strip("-.")[:60] or "disk"
    return f"vm-{vmid}-vmigrate-{slug}" + (".raw" if storage_type in FILE_BASED else "")


def size_kib(size_bytes: int) -> int:
    """Whole MiB, expressed in KiB (PVE sizes are KiB; MiB keeps every storage type happy)."""
    return -(-size_bytes // MIB) * 1024


def attached_volids(cfg: Mapping[str, Any]) -> list[str]:
    """Disk volids in device order (scsi, virtio, sata, ide; by index), CD-ROMs and cloud-init skipped."""
    rows = []
    for k, v in cfg.items():
        m = DISK_KEY.match(k)
        if not m or "media=cdrom" in str(v) or "cloudinit" in str(v):
            continue
        rows.append((_BUS_ORDER[m.group(1)], int(m.group(2)), str(v).split(",")[0]))
    return [v for _, _, v in sorted(rows)]


def ostype(spec: VmSpec, override: str | None = None) -> str:
    if override:
        return override
    if spec.guest_os.family == OsFamily.WINDOWS:
        v = str(spec.guest_os.version or "")
        return "win11" if "11" in v or "2022" in v or "2025" in v else "win10"
    return "l26" if spec.guest_os.family == OsFamily.LINUX else "other"


def bridge_for(network: str, cfg: Mapping[str, Any]) -> str:
    m = {**(cfg.get("network_map") or {})}
    br = m.get(network) or cfg.get("default_bridge")
    if not br:
        raise ConfigError(f"proxmox: no network_map entry for source network {network!r} (and no default_bridge)")
    return str(br)


def net_value(bridge: str, mac: str | None) -> str:
    br, _, vlan = bridge.partition(":")
    v = "virtio" + (f"={mac.upper()}" if mac else "") + f",bridge={br}"
    return v + (f",tag={vlan}" if vlan else "")


def vm_config(spec: VmSpec, volids: list[str], cfg: Mapping[str, Any], *, storage: str, bus: DiskBus) -> dict[str, Any]:
    """The PVE config for the migrated VM (POST /nodes/<node>/qemu/<vmid>/config)."""
    if len(volids) > 31:
        raise ProviderError(f"proxmox: {len(volids)} disks; a VM has at most 31 SCSI disks")
    prefix = {"scsi": "scsi", "virtio": "virtio", "sata": "sata", "ide": "ide"}.get(bus.value, "scsi")
    out: dict[str, Any] = {"name": spec.name, "sockets": spec.cpu.sockets, "cores": spec.cpu.cores,
                           "memory": max(64, spec.memory_bytes >> 20), "ostype": ostype(spec, cfg.get("ostype")),
                           "scsihw": "virtio-scsi-pci", "agent": "1", "tags": TAG,
                           "description": f"migrated by vmigrate from {spec.source_id}",
                           "boot": f"order={prefix}0"}
    if cfg.get("cpu_type"):
        out["cpu"] = cfg["cpu_type"]
    for i, v in enumerate(volids):
        out[f"{prefix}{i}"] = v + (",discard=on" if prefix in ("scsi", "virtio") else "")
    for i, nic in enumerate(spec.nics):
        out[f"net{i}"] = net_value(bridge_for(nic.network, cfg), nic.mac if cfg.get("preserve_mac") else None)
    if spec.firmware == Firmware.UEFI:
        out.update(bios="ovmf", machine="q35",
                   efidisk0=f"{storage}:1,efitype=4m,pre-enrolled-keys={1 if spec.secure_boot else 0}")
        if spec.secure_boot and spec.guest_os.family == OsFamily.WINDOWS:
            out["tpmstate0"] = f"{storage}:1,version=v2.0"
    return out


# ------------------------------------------------------------------ the target
class ProxmoxTarget(TargetProvider):
    type_name = "proxmox"
    platform_family = "kvm"

    def __init__(self, name: str, config: Mapping[str, Any], ctx: RunContext, api: Any = None,
                 shell_factory: Any = None):
        super().__init__(name, config, ctx)
        for k in ("url", "token_id"):
            if not config.get(k):
                raise ConfigError(f"proxmox target {name}: missing {k!r}")
        self._api, self._shell_factory = api, shell_factory
        self._shell: Any = None
        self._node: str | None = None
        self._stype: str | None = None
        self._holder: int | None = None
        self._lock = threading.Lock()
        self._paths: dict[str, str] = {}
        self._mapped: dict[str, str] = {}               # volid -> /dev/rbdN we mapped
        self._bridges: dict[str, UnixBridge] = {}
        self._sockdir: str | None = None
        self.poll = float(config.get("poll_interval", 5))

    # ------------------------------------------------------------------ plumbing
    @property
    def api(self) -> Any:
        if self._api is None:
            from .api import PveApi
            self._api = PveApi(self.config["url"], self.config["token_id"], self.config.get("token_secret", ""),
                               verify_tls=bool(self.config.get("verify_tls", True)))
        return self._api

    @property
    def node(self) -> str:
        if self._node is None:
            want = self.config.get("target_node")
            nodes = self.api.nodes()
            if want:
                if not any(n.get("node") == want for n in nodes):
                    raise ConfigError(f"proxmox: target_node {want!r} not found")
                self._node = str(want)
            else:
                up = sorted(n["node"] for n in nodes if n.get("status") == "online")
                if not up:
                    raise ProviderError("proxmox: no online node")
                self._node = up[0]
        return self._node

    @property
    def storage(self) -> str:
        s = self.config.get("target_storage")
        if not s:
            raise ConfigError("proxmox target: set target_storage (an images storage on the target node)")
        return str(s)

    @property
    def storage_type(self) -> str:
        if self._stype is None:
            self._stype = str(self.api.storage(self.storage).get("type", ""))
        return self._stype

    @property
    def shell(self) -> Any:
        if self._shell is None:
            if self._shell_factory is not None:
                self._shell = self._shell_factory(self.node)
            else:
                from ..common.remote import SshNode
                host = (self.config.get("node_addresses") or {}).get(self.node, self.node)
                self._shell = SshNode(host, self.config.get("ssh_user", "root"), self.config.get("ssh_private_key", ""),
                                      self.config.get("ssh_known_hosts", ""), port=int(self.config.get("ssh_port", 22)))
        return self._shell

    def _vm_cfg(self, vmid: int) -> dict[str, Any]:
        return self.api.config(self.node, vmid) or {}

    def _ours(self, vmid: int) -> bool:
        try:
            return TAG in tags_of(self._vm_cfg(vmid))
        except ProviderError:
            return False

    # ------------------------------------------------------------------ checks + wizard lists
    def check_connection(self) -> list[str]:
        try:
            self.api.version()
            node = self.node
        except Exception as e:  # noqa: BLE001
            return [f"cannot reach the Proxmox API: {e}"]
        p: list[str] = []
        if not self.config.get("target_storage"):
            p.append("set target_storage (an images storage on the target node)")
        else:
            st = {s.get("storage"): s for s in self.api.node_storages(node)}
            s = st.get(self.storage)
            if s is None:
                p.append(f"storage {self.storage!r} is not enabled on node {node}")
            else:
                if "images" not in str(s.get("content", "")):
                    p.append(f"storage {self.storage!r} does not hold VM images (content: {s.get('content')})")
                if s.get("type") not in SUPPORTED:
                    p.append(f"storage type {s.get('type')!r} is not supported as a target "
                             f"(use one of {', '.join(sorted(SUPPORTED))})")
        for k in ("ssh_private_key", "ssh_known_hosts"):
            if not self.config.get(k):
                p.append(f"set {k} (disks are written over SSH on node {node})")
        return p

    def preflight(self, spec: VmSpec | None = None) -> list[str]:
        p = self.check_connection()
        if p:
            return p
        try:
            out = self.shell.run(["python3", "-c", "print('ok')"], timeout=60)
            if "ok" not in out:
                p.append(f"python3 did not run on node {self.node}")
        except Exception as e:  # noqa: BLE001
            p.append(f"SSH to node {self.node} failed: {e}")
        if spec is None:
            return p
        bridges = {n.get("iface") for n in self.api.node_networks(self.node) if n.get("type") in ("bridge", "OVSBridge")}
        o = self._opts(spec)
        for nic in spec.nics:
            try:
                br = bridge_for(nic.network, o).partition(":")[0]
            except ConfigError as e:
                p.append(str(e))
                continue
            if br not in bridges:
                p.append(f"bridge {br!r} (for {nic.network!r}) not found on node {self.node}")
        need = sum(size_kib(d.size_bytes) * 1024 for d in spec.disks)
        avail = int(self.api.storage_status(self.node, self.storage).get("avail") or 0)
        if avail and need > avail:
            msg = f"storage {self.storage} has {avail >> 30} GiB free; the disks are {need >> 30} GiB provisioned"
            if self.storage_type in THIN:
                self.ctx.logger.warning("%s (thin storage: only written data is used)", msg)
            else:
                p.append(msg)
        return p

    def _opts(self, spec: VmSpec) -> dict[str, Any]:
        t = spec.extra.get("target_options") or {}
        return {**self.config, **t, "network_map": {**(self.config.get("network_map") or {}), **(t.get("network_map") or {})}}

    def list_networks(self) -> list[dict[str, Any]]:
        return sorted(({"id": n["iface"], "name": n["iface"]} for n in self.api.node_networks(self.node)
                       if n.get("type") in ("bridge", "OVSBridge")), key=lambda n: n["name"])

    def list_storage(self) -> list[dict[str, Any]]:
        return sorted(({"id": s["storage"], "name": f"{s['storage']} ({s.get('type')})"}
                       for s in self.api.node_storages(self.node)
                       if "images" in str(s.get("content", "")) and s.get("type") in SUPPORTED), key=lambda s: s["id"])

    # ------------------------------------------------------------------ holder VM + disks
    def _holder_name(self) -> str:
        return f"vmigrate-holder-{(self.ctx.run_id or 'run')[:12]}"

    def _ensure_holder(self) -> int:
        with self._lock:
            if self._holder is not None:
                return self._holder
            name = self._holder_name()
            hit = [v for v in self.api.all_vms() if v.get("name") == name and v.get("node") == self.node]
            if hit:
                vmid = int(hit[0]["vmid"])
                if not self._ours(vmid):
                    raise SafetyGateError(f"VM {vmid} is named {name} but is not tagged {TAG}")
                self._holder = vmid
                return vmid
            for _ in range(5):
                vmid = self.api.next_vmid()
                self.ctx.require_execute("proxmox create holder VM", vmid=vmid, name=name, node=self.node)
                try:
                    self.api.create_vm(self.node, vmid, name=name, tags=f"{TAG};{HOLDER_TAG}", memory=512,
                                       description="vmigrate: holds the disks of a migration in progress; becomes "
                                                   "the migrated VM (safe to delete if no migration runs)")
                except ProviderError as e:
                    if "already exists" in str(e):
                        continue                         # another client took that ID meanwhile
                    raise
                self._holder = vmid
                return vmid
            raise ProviderError("proxmox: could not reserve a VM ID")

    def create_disk(self, name: str, size_bytes: int, hints: DiskHints) -> TargetDiskRef:
        if hints.shared:
            raise ConfigError("proxmox target: shared (multi-attach) cluster disks are not supported")
        if self.storage_type not in SUPPORTED:
            raise ConfigError(f"proxmox: storage type {self.storage_type!r} cannot be a target")
        vmid = self._ensure_holder()
        fn = volume_filename(vmid, name, self.storage_type)
        kib = size_kib(size_bytes)
        self.ctx.require_execute("proxmox allocate disk", storage=self.storage, vmid=vmid, filename=fn, size_kib=kib)
        volid = self.api.alloc_disk(self.node, self.storage, vmid, fn, kib)
        return TargetDiskRef(id=volid, name=name, size_bytes=kib * 1024,
                             data={"vmid": vmid, "node": self.node, "storage": self.storage})

    def find_disk(self, name: str) -> TargetDiskRef | None:
        suffix = volume_filename(0, name, self.storage_type).split("-vmigrate-", 1)[1]
        hits = []
        for c in self.api.get(f"/nodes/{self.node}/storage/{self.storage}/content", content="images") or []:
            m = re.match(r"^[^:]+:vm-(\d+)-vmigrate-(.+)$", str(c.get("volid", "")))
            if m and m.group(2) == suffix and self._ours(int(m.group(1))):
                hits.append(c)
        if len(hits) > 1:
            raise SafetyGateError(f"several vmigrate volumes named {name!r}: {[h['volid'] for h in hits]}")
        if not hits:
            return None
        c = hits[0]
        return TargetDiskRef(id=c["volid"], name=name, size_bytes=int(c["size"]),
                             data={"vmid": int(c["vmid"]), "node": self.node, "storage": self.storage})

    # ------------------------------------------------------------------ data path
    def _path(self, volid: str) -> str:
        if volid in self._paths:
            return self._paths[volid]
        path = self.shell.run(["pvesm", "path", volid]).strip()
        if path.startswith("rbd:"):
            img, *opts = path[4:].split(":")
            argv = ["rbd", "map", img]
            for o in opts:
                k, _, v = o.partition("=")
                if k in ("conf", "id", "keyring"):
                    argv += [f"--{k}", v]
            path = self.shell.run(argv).strip()
            self._mapped[volid] = path
        if not path.startswith("/"):
            raise ProviderError(f"proxmox: cannot write {volid}: unexpected path {path!r}")
        self._paths[volid] = path
        return path

    def _agent(self, disk: TargetDiskRef, *, writable: bool) -> NbdClient:
        argv = ["python3", "-c", _AGENT_SRC, self._path(disk.id), "rw" if writable else "ro"]
        return NbdClient.over_process(self.shell.spawn(argv))

    def open_disk_writer(self, disk: TargetDiskRef, *, resume: bool = False, incremental: bool = False) -> DiskWriter:
        self.ctx.require_execute("proxmox write disk over SSH", disk=disk.name, volid=disk.id, incremental=incremental)
        zeroed = not incremental and bool(self.config.get("assume_zeroed", self.storage_type in THIN))
        return NbdDiskWriter(self._agent(disk, writable=True), disk.size_bytes, prezeroed=zeroed)

    def open_disk_reader(self, disk: TargetDiskRef) -> DiskReader | None:
        if not self.config.get("verify_after_commit", True):
            return None
        return NbdDiskReader(self._agent(disk, writable=False), disk.size_bytes)

    def morph_image(self, disk: TargetDiskRef) -> tuple[Path, str] | None:
        if disk.id not in self._bridges:
            path = self._path(disk.id)
            if self._sockdir is None:
                self._sockdir = tempfile.mkdtemp(prefix="vmpve-", dir="/tmp" if os.path.isdir("/tmp") else None)
            sh = self.shell
            self._bridges[disk.id] = UnixBridge(Path(self._sockdir) / f"{len(self._bridges)}.sock",
                                                lambda: sh.spawn(["python3", "-c", _AGENT_SRC, path, "rw"]))
        return (self._bridges[disk.id].uri, "raw")  # type: ignore[return-value]

    def _unmap(self, volid: str | None = None) -> None:
        for b in self._bridges.values():
            b.close()
        self._bridges.clear()
        for v, dev in list(self._mapped.items()):
            if volid is None or v == volid:
                try:
                    self.shell.run(["rbd", "unmap", dev])
                except ProviderError as e:
                    self.ctx.logger.warning("proxmox: rbd unmap %s: %s", dev, e)
                self._mapped.pop(v, None)
                self._paths.pop(v, None)

    def close(self) -> None:
        self._unmap()
        if self._sockdir:
            import shutil
            shutil.rmtree(self._sockdir, ignore_errors=True)
            self._sockdir = None
        if self._shell is not None and hasattr(self._shell, "close"):
            self._shell.close()
            self._shell = None

    # ------------------------------------------------------------------ replica
    def supports_replica(self) -> bool:
        return True

    def clone_disk(self, disk: TargetDiskRef, name: str, restore_point: str | None = None) -> TargetDiskRef:
        if restore_point:
            raise ProviderError("proxmox: restore points are not supported; deploy from the latest sync")
        if not self._ours(int(disk.data.get("vmid") or 0)):
            raise SafetyGateError(f"refusing to clone {disk.id}: its VM is not tagged {TAG}")
        new = self.create_disk(name, disk.size_bytes, DiskHints(bus=DiskBus.SCSI, is_boot=False, order=0))
        src, dst = self._path(disk.id), self._path(new.id)
        self.ctx.require_execute("proxmox clone disk (qemu-img convert)", source=disk.id, target=new.id)
        self.shell.run(["qemu-img", "convert", "-n", "-f", "raw", "-O", "raw", "-t", "none", "-T", "none", src, dst],
                       timeout=7 * 24 * 3600)
        return new

    def delete_disk(self, disk: TargetDiskRef) -> None:
        vmid = int(disk.data.get("vmid") or 0)
        if not self._ours(vmid):
            raise SafetyGateError(f"refusing to delete {disk.id}: its VM {vmid} is not tagged {TAG}")
        self.ctx.require_execute("proxmox delete disk", volid=disk.id)
        self._unmap(disk.id)
        self.api.delete_volume(self.node, disk.data.get("storage", self.storage), disk.id)
        self._drop_empty_holder(vmid)

    def _drop_empty_holder(self, vmid: int) -> None:
        cfg = self._vm_cfg(vmid)
        if HOLDER_TAG not in tags_of(cfg):
            return
        left = [c for c in self.api.content(self.node, self.storage, vmid) or []]
        if not left:
            self.ctx.logger.info("proxmox: removing empty holder VM %s", vmid)
            self.api.delete_vm(self.node, vmid)

    # ------------------------------------------------------------------ VM
    def target_bus(self, spec: VmSpec) -> DiskBus:
        return DiskBus(self._opts(spec).get("boot_bus", "scsi"))

    def create_vm(self, spec: VmSpec, boot_disk: TargetDiskRef, data_disks: list[TargetDiskRef]) -> VmRef:
        self._unmap()                                    # rbd devices + bridges released before the VM uses them
        disks = [boot_disk, *data_disks]
        owners = {int(d.data.get("vmid") or 0) for d in disks}
        if len(owners) != 1:
            raise ProviderError(f"proxmox: the disks belong to several VMs {sorted(owners)}; cannot build one VM")
        vmid = owners.pop()
        if not self._ours(vmid):
            raise SafetyGateError(f"refusing to configure VM {vmid}: not tagged {TAG}")
        cfg = vm_config(spec, [d.id for d in disks], self._opts(spec), storage=self.storage,
                        bus=self.target_bus(spec))
        self.ctx.require_execute("proxmox configure VM", vmid=vmid, name=spec.name, disks=len(disks))
        self.api.set_config(self.node, vmid, **cfg)
        if self._opts(spec).get("power_on", True):
            self.ctx.require_execute("proxmox start VM", vmid=vmid)
            self.api.start(self.node, vmid)
        return VmRef(str(vmid), spec.name, self.name)

    def find_vm(self, name: str) -> VmRef | None:
        hit = [v for v in self.api.all_vms() if v.get("name") == name]
        if len(hit) > 1:
            raise SafetyGateError(f"proxmox: several VMs named {name!r}")
        return VmRef(str(hit[0]["vmid"]), name, self.name) if hit else None

    def attach_disk(self, vm: VmRef, disk: TargetDiskRef, order: int) -> None:
        if not self._ours(int(vm.id)):
            raise SafetyGateError(f"refusing to change VM {vm.id}: not tagged {TAG}")
        self.ctx.require_execute("proxmox attach disk", vm=vm.name, volid=disk.id, order=order)
        self.api.set_config(self.node, int(vm.id), **{f"scsi{order}": disk.id})

    def list_attached_disks(self, vm: VmRef) -> list[str]:
        return attached_volids(self._vm_cfg(int(vm.id)))

    def check_boot(self, vm: VmRef, timeout: float = 900, *, guest_os: str | None = None,
                   progress: Any = None) -> BootCheck:
        """Running + qemu guest agent answering = booted. Guests without the agent pass after `boot_grace`
        seconds running (default 180), with a note to confirm in the Proxmox console."""
        say = progress or self.ctx.logger.info
        grace = float(self.config.get("boot_grace", 180))
        start = time.monotonic()
        last = -1e9
        while time.monotonic() - start < timeout:
            st = self.api.status(self.node, int(vm.id))
            el = time.monotonic() - start
            if st == "stopped" and el > 10:
                return BootCheck(False, "VM stopped during the boot check (guest shut down or crashed)")
            if st == "running" and self.api.agent_ping(self.node, int(vm.id)):
                return BootCheck(True, "running; the qemu guest agent answered")
            if st == "running" and el >= grace:
                return BootCheck(True, f"running for {int(el)}s; no qemu guest agent answered (not installed?). "
                                       "Confirm the login screen in the Proxmox console")
            if el - last >= 60:
                last = el
                say(f"boot check: VM {st or '?'}, {int(el)}s of {int(timeout)}s; waiting for the guest agent "
                    f"(passes after {int(grace)}s running)")
            cancel.sleep(self.poll)
        return BootCheck(False, f"VM not confirmed booted within {int(timeout)}s")

    # ------------------------------------------------------------------ cleanup
    def cleanup_on_failure(self, created: Mapping[str, Any]) -> None:
        self.ctx.require_execute("proxmox cleanup created resources")
        self._unmap()
        vmids: set[int] = set()
        vm = created.get("target_vm")
        if vm:
            vmids.add(int(vm["id"]))
        for d in created.get("target_disks", []):
            if (d.get("data") or {}).get("vmid"):
                vmids.add(int(d["data"]["vmid"]))
        existing = {int(v["vmid"]) for v in self.api.all_vms()}
        for vmid in sorted(vmids):
            if vmid not in existing:
                self.ctx.logger.info("cleanup: VM %s is already gone", vmid)
                continue
            if not self._ours(vmid):
                raise SafetyGateError(f"refusing to delete VM {vmid}: not tagged {TAG}")
            if self.api.status(self.node, vmid) == "running":
                self.api.shutdown(self.node, vmid, force=True, timeout=60)
            self.api.delete_vm(self.node, vmid)          # its own volumes go with it
