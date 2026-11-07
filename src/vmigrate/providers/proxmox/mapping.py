"""Purpose: PURE mapping from a Proxmox VE qemu VM config (GET /nodes/{node}/qemu/{vmid}/config) to the
IR, the storage-content cross-check, and the per-storage "how do I expose a snapshot" plan. No I/O.

Usage:
    disks = config_disks(cfg)                    # every attached data disk (no cap), refusals applied
    spec = to_vmspec(vm, cfg, content_sizes)     # content_sizes: {volid: exact bytes} from storage content
    crosscheck(cfg, content)                     # {"pve.config": n, "pve.storage_content": m}
    plan = expose_plan(volid, storage_type, path, fmt, snapname)   # see node.py

Rules:
  * disk keys scsiN / virtioN / sataN / ideN; cdroms (media=cdrom), cloud-init drives, unusedN,
    efidisk0 and tpmstate0 are not guest data disks (EFI vars/TPM state are noted, not copied)
  * pass-through disks (/dev/... with no storage) are REFUSED: snapshots cannot include them
  * storage types without snapshots (lvm thick, raw files on dir/nfs, iscsi) are REFUSED with the reason
  * controller model recorded from scsihw (virtio-scsi, lsilogic, megasas, pvscsi), virtio-blk, ahci, ide
"""

from __future__ import annotations

import re
from typing import Any

from ...ir import CpuSpec, DiskBus, DiskSpec, Firmware, GuestOS, NicSpec, OsFamily, VmSpec


class ProxmoxMappingError(ValueError):
    pass


DISK_KEY = re.compile(r"^(scsi|virtio|sata|ide)(\d+)$")
_BUS = {"scsi": DiskBus.SCSI, "virtio": DiskBus.VIRTIO, "sata": DiskBus.SATA, "ide": DiskBus.IDE}
_RANK = {"scsi": 0, "virtio": 1, "sata": 2, "ide": 3}
SCSIHW = {"virtio-scsi-pci": "virtio-scsi", "virtio-scsi-single": "virtio-scsi", "lsi": "lsilogic",
          "lsi53c810": "lsilogic", "megasas": "megasas", "pvscsi": "pvscsi"}
NIC_MODELS = ("virtio", "e1000", "e1000e", "vmxnet3", "rtl8139", "i82551", "i82557b", "i82559er", "ne2k_pci", "pcnet")
SNAPSHOT_STORAGES = {"lvmthin", "zfspool", "rbd", "dir", "nfs", "cifs", "glusterfs", "cephfs", "btrfs"}
FILE_STORAGES = {"dir", "nfs", "cifs", "glusterfs", "cephfs", "btrfs"}
_SIZE = re.compile(r"^(\d+(?:\.\d+)?)([KMGT]?)$", re.I)
_MULT = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
SAFE_NAME = re.compile(r"^[A-Za-z0-9/][A-Za-z0-9_.+/:@=-]*$")   # no spaces or shell metacharacters


def parse_props(s: str) -> tuple[str, dict[str, str]]:
    """'local-lvm:vm-100-disk-0,size=32G,iothread=1' -> ('local-lvm:vm-100-disk-0', {'size': '32G', ...})."""
    parts = [p for p in str(s).split(",") if p]
    head, props = "", {}
    for i, p in enumerate(parts):
        if "=" in p and not (i == 0 and ":" in p.split("=", 1)[0]):
            k, v = p.split("=", 1)
            props[k.strip()] = v.strip()
        elif i == 0:
            head = p.strip()
    return head, props


def parse_size(s: str | None) -> int:
    m = _SIZE.match((s or "").strip())
    if not m:
        return 0
    return int(float(m.group(1)) * _MULT[m.group(2).upper()])


def config_disks(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for key, val in cfg.items():
        m = DISK_KEY.match(key)
        if not m:
            continue
        head, props = parse_props(val)
        if props.get("media") == "cdrom" or head == "none" or "cloudinit" in head:
            continue
        if head.startswith("/") or ":" not in head:
            raise ProxmoxMappingError(f"{key} is a pass-through disk ({head}); snapshots cannot include it. "
                                      "Move it to a VM disk on snapshot-capable storage first.")
        storage, volname = head.split(":", 1)
        out.append({"key": key, "bus": m.group(1), "index": int(m.group(2)), "volid": head, "storage": storage,
                    "volname": volname, "size": parse_size(props.get("size")), "props": props})
    out.sort(key=lambda d: (_RANK[d["bus"]], d["index"]))
    return out


def owned_content(content: list[dict[str, Any]], vmid: int, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Storage content entries that are this VM's attached data disks (the independent enumeration):
    everything the storages list for vmid, minus unused, EFI/TPM state, cloud-init and vmstate."""
    special = set()
    for k, v in cfg.items():
        if k.startswith(("unused", "efidisk", "tpmstate", "vmstate")) or "cloudinit" in str(v):
            special.add(parse_props(v)[0])
    out = []
    for c in content:
        if int(c.get("vmid") or -1) != vmid or c.get("content", "images") != "images":
            continue
        v = c.get("volid", "")
        if v in special or "cloudinit" in v or "-state-" in v or v.endswith("-disk-efi"):
            continue
        out.append(c)
    return out


def crosscheck(cfg: dict[str, Any], content: list[dict[str, Any]], vmid: int) -> dict[str, int]:
    return {"pve.config": len(config_disks(cfg)), "pve.storage_content": len(owned_content(content, vmid, cfg))}


def guest_os(ostype: str | None, agent_os: dict[str, Any] | None = None) -> GuestOS:
    t = (ostype or "").lower()
    if t.startswith(("win", "w2k", "wxp", "wvista")):
        return GuestOS(OsFamily.WINDOWS, distro="windows", version=t)
    if t in ("l26", "l24"):
        distro = (agent_os or {}).get("id")
        return GuestOS(OsFamily.LINUX, distro=distro, version=(agent_os or {}).get("version-id"))
    return GuestOS(OsFamily.UNKNOWN)


def _boot_key(cfg: dict[str, Any], keys: list[str]) -> str:
    boot = str(cfg.get("boot") or "")
    if boot.startswith("order="):
        for k in boot[len("order="):].split(";"):
            if k in keys:
                return k
    if cfg.get("bootdisk") in keys:
        return cfg["bootdisk"]
    return keys[0]


def _memory_mib(v: Any) -> int:
    if isinstance(v, (int, float)):
        return int(v)
    head, props = parse_props(str(v))
    return int(props.get("current") or head or 512)


def to_vmspec(vm: dict[str, Any], cfg: dict[str, Any], content_sizes: dict[str, int] | None = None,
              agent_os: dict[str, Any] | None = None) -> VmSpec:
    name = cfg.get("name") or vm.get("name") or f"vm-{vm['vmid']}"
    raw = config_disks(cfg)
    if not raw:
        raise ProxmoxMappingError(f"{name}: VM has no data disks")
    keys = [d["key"] for d in raw]
    boot = _boot_key(cfg, keys)
    scsihw = SCSIHW.get(str(cfg.get("scsihw") or "lsi"), str(cfg.get("scsihw")))
    model = {"scsi": scsihw, "virtio": "virtio-blk", "sata": "ahci", "ide": "ide"}
    disks = []
    for order, d in enumerate(raw):
        size = (content_sizes or {}).get(d["volid"]) or d["size"]
        if size <= 0:
            raise ProxmoxMappingError(f"{name}: {d['key']} ({d['volid']}) has no known size")
        disks.append(DiskSpec(id=d["volid"], bus=_BUS[d["bus"]], size_bytes=size, order=order, is_boot=d["key"] == boot,
                              bus_index=d["index"], label=d["key"], controller=model[d["bus"]],
                              source_handle={"key": d["key"], "volid": d["volid"], "storage": d["storage"]}))
    nics = []
    for k in sorted((k for k in cfg if re.match(r"^net\d+$", k)), key=lambda k: int(k[3:])):
        head, props = parse_props(cfg[k])
        mdl = next((m for m in NIC_MODELS if m in props), None)
        if mdl is None and "=" in str(cfg[k]).split(",")[0]:
            mdl, mac = str(cfg[k]).split(",")[0].split("=", 1)
        else:
            mac = props.get(mdl or "", None)
        net = props.get("bridge", "")
        if props.get("tag"):
            net = f"{net}/vlan{props['tag']}"
        nics.append(NicSpec(id=k, network=net, mac=mac or props.get("macaddr"), model=mdl))
    efi = cfg.get("efidisk0")
    spec = VmSpec(
        name=name, source_id=str(vm["vmid"]),
        cpu=CpuSpec(sockets=int(cfg.get("sockets") or 1), cores=int(cfg.get("cores") or 1)),
        memory_bytes=_memory_mib(cfg.get("memory", 512)) << 20, disks=disks, nics=nics,
        firmware=Firmware.UEFI if cfg.get("bios") == "ovmf" else Firmware.BIOS,
        secure_boot=bool(efi and parse_props(efi)[1].get("pre-enrolled-keys") == "1"),
        guest_os=guest_os(cfg.get("ostype"), agent_os), machine_type=cfg.get("machine"),
        extra={"proxmox": {"node": vm.get("node"), "vmid": vm["vmid"], "agent": agent_enabled(cfg),
                           "efidisk": bool(efi), "tpm": bool(cfg.get("tpmstate0")), "source_platform": "proxmox"}})
    spec.validate()
    return spec


def agent_enabled(cfg: dict[str, Any]) -> bool:
    head, props = parse_props(str(cfg.get("agent") or "0"))
    return (props.get("enabled") or head) in ("1", "true", "on")


def safe(name: str, what: str) -> str:
    if not SAFE_NAME.match(name) or ".." in name:
        raise ProxmoxMappingError(f"refusing unsafe {what}: {name!r}")
    return name


def rbd_spec_with_snap(path: str, snap: str) -> str:
    """'rbd:pool/vm-100-disk-0:conf=/etc/pve/ceph.conf:id=admin' + snap -> 'rbd:pool/vm-100-disk-0@snap:conf=...'."""
    if not path.startswith("rbd:"):
        raise ProxmoxMappingError(f"not an rbd path: {path!r}")
    rest = path[4:]
    image, sep, opts = rest.partition(":")
    return f"rbd:{image}@{snap}{sep}{opts}"


def expose_plan(volid: str, storage_type: str, path: str, fmt: str, snap: str, tag: str) -> dict[str, Any]:
    """How the node exposes `volid` AS OF snapshot `snap` to qemu-nbd. `tag` names temporary objects
    (must start with vmigrate-). Returns {setup: [argv...], nbd_target, nbd_format, nbd_extra: [...],
    teardown: [argv...]}; argv lists are executed without a shell."""
    if not tag.startswith("vmigrate-"):
        raise ProxmoxMappingError("temporary object names must start with vmigrate-")
    safe(snap, "snapshot name")
    safe(path, "volume path")
    if storage_type == "lvmthin":
        dev = path.rsplit("/", 1)
        if len(dev) != 2 or not path.startswith("/dev/"):
            raise ProxmoxMappingError(f"unexpected lvm path {path!r}")
        vg = dev[0][len("/dev/"):]
        lv = f"snap_{dev[1]}_{snap}"
        return {"setup": [["lvchange", "-ay", "-Ky", f"{vg}/{lv}"]], "nbd_target": f"/dev/{vg}/{lv}",
                "nbd_format": "raw", "nbd_extra": [], "teardown": [["lvchange", "-an", f"{vg}/{lv}"]]}
    if storage_type == "zfspool":
        if not path.startswith("/dev/zvol/"):
            raise ProxmoxMappingError(f"unexpected zvol path {path!r}")
        ds = path[len("/dev/zvol/"):]
        pool = ds.split("/", 1)[0]
        clone = f"{pool}/{tag}"
        return {"setup": [["zfs", "clone", "-o", "readonly=on", f"{ds}@{snap}", clone], ["udevadm", "settle"]],
                "nbd_target": f"/dev/zvol/{clone}", "nbd_format": "raw", "nbd_extra": [],
                "teardown": [["zfs", "destroy", clone]]}
    if storage_type == "rbd":
        return {"setup": [], "nbd_target": rbd_spec_with_snap(path, snap), "nbd_format": "raw", "nbd_extra": [],
                "teardown": []}
    if storage_type in FILE_STORAGES:
        if fmt != "qcow2":
            raise ProxmoxMappingError(f"{volid}: {fmt} files on {storage_type} storage cannot be snapshotted; "
                                      "move the disk to qcow2 or a snapshot-capable storage")
        return {"setup": [], "nbd_target": path, "nbd_format": "qcow2",
                "nbd_extra": ["--force-share", f"--load-snapshot=snapshot.name={snap}"], "teardown": []}
    raise ProxmoxMappingError(f"{volid}: storage type {storage_type!r} does not support snapshots "
                              f"(supported: {sorted(SNAPSHOT_STORAGES)})")
