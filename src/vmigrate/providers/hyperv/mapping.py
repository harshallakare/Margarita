"""Purpose: PURE mapping from a Hyper-V VM description (JSON produced by the PowerShell scripts in
client.py) to the IR, plus Windows-path -> worker-path mapping and RCT paging. No I/O.

Usage:
    spec = to_vmspec(info)                 # raises HypervMappingError (fail loud) on anything unsafe
    crosscheck(info)                       # {"hyperv.Get-VMHardDiskDrive": n, "hyperv.wmi.StorageAllocationSettingData": m}
    local_path(r"C:\\ClusterStorage\\Volume1\\vm\\d.vhdx", share_map)   # -> /mnt/csv1/vm/d.vhdx
    changed_ranges(query, capacity)        # page through Msvm_ImageManagementService.GetVirtualDiskChanges

`info` shape: {id, name, generation, cpu, memory_bytes, secure_boot, boot_disk_id, os_name,
    version, disks: [{id, controller_type: IDE|SCSI, controller_number, controller_location, path,
    disk_number, shared, format, vhd_type, size, parent}], nics: [{name, switch, mac, legacy}],
    wmi_disk_count}

Rules:
  * every attached hard disk counts — cross-checked against WMI's logical-disk allocations
  * pass-through disks (DiskNumber, no file) are REFUSED: not included in checkpoints
  * shared VHDX / VHD Set (persistent reservations) are REFUSED: excluded from checkpoints
  * differencing disks are allowed at inspect (the VM may run on a checkpoint) but the snapshot
    read path refuses them unless the frozen file is chain-free (see source.open_disk)
  * controller model recorded as hyperv-scsi / hyperv-ide so OSMorph injects virtio
"""

from __future__ import annotations

import ntpath
from collections.abc import Callable
from typing import Any

from ...core.ranges import ByteRange, normalize
from ...ir import CpuSpec, DiskBus, DiskSpec, Firmware, GuestOS, NicSpec, OsFamily, VmSpec


class HypervMappingError(ValueError):
    pass


def as_list(v: Any) -> list[Any]:
    """PowerShell's ConvertTo-Json unwraps 1-element arrays and emits null for empty ones."""
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


_CTRL = {"IDE": (DiskBus.IDE, "hyperv-ide", 2), "SCSI": (DiskBus.SCSI, "hyperv-scsi", 64)}
_RANK = {DiskBus.IDE: 0, DiskBus.SCSI: 1}   # Gen1 boots from IDE 0:0 -> keep IDE first


def guest_os(os_name: str | None) -> GuestOS:
    n = (os_name or "").lower()
    if "windows" in n:
        return GuestOS(OsFamily.WINDOWS, distro="windows", version=os_name)
    for key, distro in (("red hat", "rhel"), ("centos", "centos"), ("rocky", "rocky"), ("alma", "almalinux"),
                        ("oracle", "oracle"), ("ubuntu", "ubuntu"), ("debian", "debian"), ("suse", "sles"),
                        ("fedora", "fedora"), ("linux", None)):
        if key in n:
            return GuestOS(OsFamily.LINUX, distro=distro, version=os_name)
    return GuestOS(OsFamily.UNKNOWN)


def crosscheck(info: dict[str, Any]) -> dict[str, int]:
    return {"hyperv.Get-VMHardDiskDrive": len(as_list(info.get("disks"))),
            "hyperv.wmi.StorageAllocationSettingData": int(info.get("wmi_disk_count") or 0)}


def to_vmspec(info: dict[str, Any]) -> VmSpec:
    name = info["name"]
    raw = as_list(info.get("disks"))
    if not raw:
        raise HypervMappingError(f"{name}: VM has no hard disks")
    rows = []
    for d in raw:
        where = f"{name}: {d.get('controller_type')} {d.get('controller_number')}:{d.get('controller_location')}"
        if not d.get("path"):
            raise HypervMappingError(f"{where} is a pass-through disk (physical disk {d.get('disk_number')}); "
                                     "checkpoints cannot include it. Convert it to a VHDX first.")
        if d.get("shared"):
            raise HypervMappingError(f"{where} is a shared VHDX / VHD Set; checkpoints exclude it, so it could "
                                     "only be read live")
        if d.get("controller_type") not in _CTRL:
            raise HypervMappingError(f"{where}: unknown controller type")
        if not d.get("size"):
            raise HypervMappingError(f"{where}: Get-VHD returned no size for {d['path']} (file inaccessible?)")
        bus, model, per = _CTRL[d["controller_type"]]
        rows.append((_RANK[bus], int(d["controller_number"]), int(d["controller_location"]), bus, model, per, d))
    rows.sort(key=lambda r: r[:3])
    boot_id = info.get("boot_disk_id")
    ids_in_order = [r[6].get("id") for r in rows]
    if boot_id not in ids_in_order:
        boot_id = ids_in_order[0]       # Gen1: IDE 0:0 first; Gen2 without a disk boot entry: first disk
    disks = []
    for order, (_, cn, cl, bus, model, per, d) in enumerate(rows):
        disks.append(DiskSpec(
            id=d.get("id") or f"{info['id']}:{d['controller_type']}{cn}:{cl}", bus=bus, size_bytes=int(d["size"]),
            order=order, is_boot=d.get("id") == boot_id, bus_index=cn * per + cl,
            label=f"{d['controller_type'].lower()}{cn}:{cl}", controller=model,
            source_handle={"path": d["path"], "format": d.get("format"), "vhd_type": d.get("vhd_type")}))
    nics = [NicSpec(id=n.get("name") or f"nic{i}", network=n.get("switch") or "", mac=n.get("mac"),
                    model="hyperv-legacy" if n.get("legacy") else "netvsc")
            for i, n in enumerate(as_list(info.get("nics")))]
    spec = VmSpec(
        name=name, source_id=info["id"], cpu=CpuSpec(sockets=1, cores=max(1, int(info.get("cpu") or 1))),
        memory_bytes=int(info["memory_bytes"]), disks=disks, nics=nics,
        firmware=Firmware.UEFI if int(info.get("generation") or 1) == 2 else Firmware.BIOS,
        secure_boot=bool(info.get("secure_boot")), guest_os=guest_os(info.get("os_name")),
        extra={"hyperv": {"generation": info.get("generation"), "version": info.get("version"),
                          "source_platform": "hyperv"}})
    spec.validate()
    return spec


def rct_capable(info: dict[str, Any]) -> bool:
    """RCT needs VM configuration version 6.2+ (Windows Server 2016 and later)."""
    try:
        major, minor = (int(x) for x in str(info.get("version") or "0.0").split(".")[:2])
    except ValueError:
        return False
    return (major, minor) >= (6, 2)


def local_path(win_path: str, share_map: dict[str, str]) -> str:
    """Map a Hyper-V host path (C:\\..., \\\\server\\share\\...) to where this worker mounts it.
    Longest matching prefix wins; matching is case-insensitive like Windows."""
    norm = win_path.replace("/", "\\")
    best = None
    for prefix, mount in share_map.items():
        p = prefix.replace("/", "\\").rstrip("\\")
        if norm.lower() == p.lower() or norm.lower().startswith(p.lower() + "\\"):
            if best is None or len(p) > len(best[0]):
                best = (p, mount)
    if best is None:
        raise HypervMappingError(f"no share_map entry covers {win_path!r}; add one (e.g. "
                                 f"{{\"{ntpath.dirname(norm)}\": \"/mnt/hyperv/...\"}})")
    rest = norm[len(best[0]):].lstrip("\\")
    return best[1].rstrip("/") + ("/" + rest.replace("\\", "/") if rest else "")


def same_path(a: str, b: str) -> bool:
    return a.replace("/", "\\").lower() == b.replace("/", "\\").lower()


def changed_ranges(query: Callable[[int, int], tuple[list[tuple[int, int]], int]], capacity: int,
                   window: int = 1 << 40) -> list[ByteRange]:
    """GetVirtualDiskChanges(offset, length) -> ([(off, len)...], processed_bytes); it may process
    less than requested, so page until the whole disk is covered."""
    out: list[ByteRange] = []
    pos = 0
    while pos < capacity:
        areas, processed = query(pos, min(window, capacity - pos))
        if processed <= 0:
            raise HypervMappingError(f"GetVirtualDiskChanges made no progress at offset {pos}")
        out += [ByteRange(o, n) for o, n in areas if n > 0]
        pos += processed
    return normalize(out)
