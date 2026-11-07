"""Purpose: PURE mapping from a vSphere VM description (plain dicts produced by client.py) to the IR,
plus the VMware-specific safety rules. No I/O, no pyVmomi — unit-tested with canned payloads.

Usage:
    spec = to_vmspec(info)                 # raises VmwareMappingError (fail loud) on anything unsafe
    crosscheck(info)                       # {"vsphere.config.hardware.device": n, "vsphere.layoutEx.disk": m}
    flat_readable(snapshot_disk)           # may the HTTPS flat-file transport read this disk?
    changed_ranges(query, capacity)        # page through QueryChangedDiskAreas

`info` shape (see client.VsphereClient.vm_info):
    {moref, name, uuid, num_cpu, cores_per_socket, memory_mb, firmware: bios|efi, secure_boot,
     guest_id, guest_full_name, cbt_enabled, boot_disk_keys: [int],
     controllers: [{key, type, bus_number}],
     disks: [{key, controller_key, unit_number, capacity_bytes, file_name, datastore, backing_type,
              disk_mode, rdm_mode, parent_file, uuid, change_id}],
     nics: [{key, type, mac, network}],
     layout_disks: [{key, files: [..]}]}

Rules:
  * every VirtualDisk device counts — no cap; disk count is cross-checked against layoutEx.disk
  * independent disks are REFUSED: snapshots do not cover them, so reading one = reading a live disk
  * physical-mode RDMs are REFUSED: they cannot be snapshotted
  * the controller MODEL is recorded (pvscsi, lsilogic, ...) so OSMorph knows virtio drivers are needed
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ...core.ranges import ByteRange, normalize
from ...ir import CpuSpec, DiskBus, DiskSpec, Firmware, GuestOS, NicSpec, OsFamily, VmSpec


class VmwareMappingError(ValueError):
    pass


# pyVmomi device class (last name segment) -> (IR bus, controller model, units per bus for addressing)
CONTROLLERS: dict[str, tuple[DiskBus, str, int]] = {
    "ParaVirtualSCSIController": (DiskBus.SCSI, "pvscsi", 16),
    "VirtualLsiLogicController": (DiskBus.SCSI, "lsilogic", 16),
    "VirtualLsiLogicSASController": (DiskBus.SCSI, "lsilogic-sas", 16),
    "VirtualBusLogicController": (DiskBus.SCSI, "buslogic", 16),
    "VirtualAHCIController": (DiskBus.SATA, "ahci", 30),
    "VirtualIDEController": (DiskBus.IDE, "ide", 2),
    "VirtualNVMEController": (DiskBus.NVME, "nvme", 64),
}
_BUS_RANK = {DiskBus.SCSI: 0, DiskBus.NVME: 1, DiskBus.SATA: 2, DiskBus.IDE: 3, DiskBus.VIRTIO: 4}
NIC_MODELS = {"VirtualVmxnet3": "vmxnet3", "VirtualVmxnet2": "vmxnet2", "VirtualVmxnet": "vmxnet",
              "VirtualE1000": "e1000", "VirtualE1000e": "e1000e", "VirtualPCNet32": "pcnet32",
              "VirtualSriovEthernetCard": "sriov"}
INDEPENDENT = {"independent_persistent", "independent_nonpersistent"}
_LINUX_HINTS = ("rhel", "centos", "ubuntu", "debian", "sles", "suse", "oracle", "rocky", "alma", "fedora", "linux",
                "amazonlinux", "photon", "coreos", "mandrake", "mandriva", "asianux", "turbolinux", "redhat")
_DISTROS = (("rhel", "rhel"), ("redhat", "rhel"), ("centos", "centos"), ("rocky", "rocky"), ("alma", "almalinux"),
            ("oracle", "oracle"), ("ubuntu", "ubuntu"), ("debian", "debian"), ("sles", "sles"), ("suse", "opensuse"),
            ("fedora", "fedora"), ("amazonlinux", "amzn"), ("photon", "photon"))


def guest_os(guest_id: str | None, full_name: str | None) -> GuestOS:
    gid = (guest_id or "").lower()
    bits = 64 if "64" in gid or "64-bit" in (full_name or "") else 32
    if gid.startswith("win"):
        return GuestOS(OsFamily.WINDOWS, distro="windows", version=gid.removeprefix("windows"), bits=bits)
    if any(h in gid for h in _LINUX_HINTS) or "linux" in (full_name or "").lower():
        distro = next((d for k, d in _DISTROS if k in gid), None)
        return GuestOS(OsFamily.LINUX, distro=distro, version=None, bits=bits)
    return GuestOS(OsFamily.UNKNOWN, bits=bits)


def _check_disk(d: dict[str, Any], vm: str) -> None:
    where = f"{vm}: disk {d.get('file_name')} (key {d['key']})"
    if (d.get("disk_mode") or "persistent") in INDEPENDENT:
        raise VmwareMappingError(f"{where} is {d['disk_mode']}: snapshots do not include independent disks, so it "
                                 "could only be read LIVE. Change it to dependent (persistent) mode or exclude the VM.")
    if d.get("rdm_mode") == "physicalMode":
        raise VmwareMappingError(f"{where} is a physical-mode RDM, which cannot be snapshotted. Convert it to a "
                                 "virtual-mode RDM or a VMDK first.")
    if not d.get("capacity_bytes") or d["capacity_bytes"] <= 0:
        raise VmwareMappingError(f"{where} reports no capacity")


def crosscheck(info: dict[str, Any]) -> dict[str, int]:
    """Two independent enumerations: the device list and the file layout."""
    return {"vsphere.config.hardware.device": len(info.get("disks") or []),
            "vsphere.layoutEx.disk": len(info.get("layout_disks") or [])}


def to_vmspec(info: dict[str, Any]) -> VmSpec:
    name = info["name"]
    ctrls = {c["key"]: c for c in info.get("controllers") or []}
    raw = info.get("disks") or []
    if not raw:
        raise VmwareMappingError(f"{name}: VM has no virtual disks")
    rows = []
    for d in raw:
        _check_disk(d, name)
        c = ctrls.get(d["controller_key"])
        if c is None or c["type"] not in CONTROLLERS:
            raise VmwareMappingError(f"{name}: disk key {d['key']} is on an unknown controller "
                                     f"{c['type'] if c else d['controller_key']!r}")
        bus, model, per_bus = CONTROLLERS[c["type"]]
        rows.append((bus, int(c["bus_number"]), int(d["unit_number"]), model, per_bus, d, c))
    rows.sort(key=lambda r: (_BUS_RANK[r[0]], r[1], r[2]))
    keys_in_order = [r[5]["key"] for r in rows]
    boot_key = next((k for k in info.get("boot_disk_keys") or [] if k in keys_in_order), keys_in_order[0])
    ids = set()
    disks = []
    for order, (bus, busno, unit, model, per_bus, d, c) in enumerate(rows):
        did = d.get("uuid") or f"{info['moref']}:{d['key']}"
        if did in ids:  # cloned VMs can carry duplicate disk UUIDs — fall back to device keys
            did = f"{info['moref']}:{d['key']}"
        ids.add(did)
        disks.append(DiskSpec(
            id=did, bus=bus, size_bytes=int(d["capacity_bytes"]), order=order, is_boot=d["key"] == boot_key,
            bus_index=busno * per_bus + unit, label=f"{bus.value}{busno}:{unit}", controller=model,
            source_handle={"key": d["key"], "file_name": d["file_name"], "datastore": d.get("datastore"),
                           "backing_type": d.get("backing_type")},
        ))
    nics = [NicSpec(id=str(n["key"]), network=n.get("network") or "", mac=n.get("mac"),
                    model=NIC_MODELS.get(n.get("type", ""), n.get("type"))) for n in info.get("nics") or []]
    cores = max(1, int(info.get("cores_per_socket") or 1))
    ncpu = max(1, int(info.get("num_cpu") or 1))
    if ncpu % cores:
        cores = 1
    spec = VmSpec(
        name=name, source_id=info["moref"], cpu=CpuSpec(sockets=ncpu // cores, cores=cores),
        memory_bytes=int(info["memory_mb"]) << 20, disks=disks, nics=nics,
        firmware=Firmware.UEFI if info.get("firmware") == "efi" else Firmware.BIOS,
        secure_boot=bool(info.get("secure_boot")), guest_os=guest_os(info.get("guest_id"), info.get("guest_full_name")),
        extra={"vmware": {"uuid": info.get("uuid"), "guest_id": info.get("guest_id"),
                          "cbt_enabled": bool(info.get("cbt_enabled")), "source_platform": "vmware"}},
    )
    spec.validate()
    return spec


def flat_base_path(file_name: str) -> tuple[str, str]:
    """'[ds1] vm/vm.vmdk' -> ('ds1', 'vm/vm-flat.vmdk')."""
    if not file_name.startswith("[") or "] " not in file_name:
        raise VmwareMappingError(f"unexpected datastore path {file_name!r}")
    ds, rel = file_name[1:].split("] ", 1)
    if not rel.endswith(".vmdk"):
        raise VmwareMappingError(f"not a vmdk: {file_name!r}")
    return ds, rel[: -len(".vmdk")] + "-flat.vmdk"


def flat_readable(snap_disk: dict[str, Any]) -> tuple[bool, str]:
    """The HTTPS datastore transport reads the base -flat.vmdk directly. That is only a frozen,
    complete image when, AT THE SNAPSHOT, the disk had no parent (our snapshot is the only one)
    and its backing is a flat VMDK (not vSAN/vVol/SEsparse/RDM)."""
    if snap_disk.get("backing_type") != "FlatVer2BackingInfo":
        return False, f"backing {snap_disk.get('backing_type')} has no flat file (use the vddk transport)"
    if snap_disk.get("parent_file"):
        return False, ("the disk already had snapshots before ours (delta chain), so the base file is not the "
                       "snapshot image (use the vddk transport, or consolidate existing snapshots)")
    return True, "flat base file is frozen while our snapshot exists"


def changed_ranges(query: Callable[[int], tuple[int, int, list[tuple[int, int]]]], capacity: int) -> list[ByteRange]:
    """Page through QueryChangedDiskAreas. `query(start)` returns (startOffset, length, [(start, len), ...])
    for the window it covered; vSphere may cover less than the whole disk per call."""
    out: list[ByteRange] = []
    pos = 0
    guard = 0
    while pos < capacity:
        start, length, areas = query(pos)
        if length <= 0:
            raise VmwareMappingError(f"QueryChangedDiskAreas made no progress at offset {pos}")
        out += [ByteRange(a, n) for a, n in areas if n > 0]
        pos = start + length
        guard += 1
        if guard > 1_000_000:
            raise VmwareMappingError("QueryChangedDiskAreas did not terminate")
    return normalize(out)
