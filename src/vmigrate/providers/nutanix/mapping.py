"""Purpose: PURE mapping from Prism JSON to the IR. No I/O — unit-tested against canned payloads.

Usage:
    spec = to_vmspec(v3_vm_json, v2_vm_json)        # raises NutanixMappingError on any disagreement
    disks_v3(v3_vm_json); disks_v2(v2_vm_json)       # independent enumerations for the gate

Rules (the reason this tool exists):
  * every entry of v3 spec.resources.disk_list is considered — no index cap, no slicing
  * CD-ROMs are excluded on BOTH sides with the same rule
  * volume-group-backed disks are NOT silently skipped: they raise (unsupported, fail loud)
  * v3 and v2 must agree on the exact set of (bus, index) addresses and on every size
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...ir import CpuSpec, DiskBus, DiskSpec, Firmware, GuestOS, NicSpec, OsFamily, VmSpec

_BUS = {"SCSI": DiskBus.SCSI, "SATA": DiskBus.SATA, "IDE": DiskBus.IDE, "PCI": DiskBus.VIRTIO,
        "scsi": DiskBus.SCSI, "sata": DiskBus.SATA, "ide": DiskBus.IDE, "pci": DiskBus.VIRTIO}
# what the AHV guest actually sees per bus (drives the OSMorph virtio decision)
_CONTROLLER = {DiskBus.SCSI: "virtio-scsi", DiskBus.VIRTIO: "virtio-blk", DiskBus.SATA: "ahci", DiskBus.IDE: "ide"}
_BUS_RANK = {DiskBus.SCSI: 0, DiskBus.VIRTIO: 1, DiskBus.SATA: 2, DiskBus.IDE: 3, DiskBus.NVME: 4}


class NutanixMappingError(ValueError):
    pass


@dataclass(frozen=True)
class V3Disk:
    uuid: str
    bus: DiskBus
    index: int
    size_bytes: int
    container_uuid: str | None


@dataclass(frozen=True)
class V2Disk:
    vmdisk_uuid: str                 # the storage vdisk (backs ndfs_filepath) — NOT the v3 disk uuid
    bus: DiskBus
    index: int
    size_bytes: int
    ndfs_filepath: str
    container_uuid: str | None
    device_uuid: str = ""            # the VM disk device — the same id as v3 disk_list[].uuid / v4 diskExtId


def _resources(v3: dict[str, Any]) -> dict[str, Any]:
    # spec.resources is the desired state; status.resources is the observed state. Prefer status
    # (it includes server-assigned uuids) but fall back to spec.
    return (v3.get("status") or {}).get("resources") or (v3.get("spec") or {}).get("resources") or {}


def disks_v3(v3: dict[str, Any]) -> list[V3Disk]:
    out: list[V3Disk] = []
    for d in _resources(v3).get("disk_list") or []:
        props = d.get("device_properties") or {}
        if props.get("device_type", "DISK") == "CDROM":
            continue
        addr = props.get("disk_address") or {}
        if d.get("volume_group_reference"):
            raise NutanixMappingError(
                f"disk at {addr} is a Volume Group reference — not supported yet; refusing to drop it silently")
        bus = _BUS.get(addr.get("adapter_type", ""))
        if bus is None or "device_index" not in addr:
            raise NutanixMappingError(f"v3 disk with unknown address {addr}")
        size = d.get("disk_size_bytes") or (d.get("disk_size_mib", 0) << 20)
        out.append(V3Disk(
            uuid=d.get("uuid", ""), bus=bus, index=int(addr["device_index"]), size_bytes=int(size),
            container_uuid=((d.get("storage_config") or {}).get("storage_container_reference") or {}).get("uuid"),
        ))
    return out


def disks_v2(v2: dict[str, Any]) -> list[V2Disk]:
    out: list[V2Disk] = []
    for d in v2.get("vm_disk_info") or []:
        if d.get("is_cdrom"):
            continue
        a = d.get("disk_address") or {}
        if a.get("volume_group_uuid"):
            raise NutanixMappingError(f"v2 disk {a} is volume-group backed — unsupported, refusing to drop it")
        bus = _BUS.get(a.get("device_bus", ""))
        if bus is None:
            raise NutanixMappingError(f"v2 disk with unknown bus {a}")
        out.append(V2Disk(
            vmdisk_uuid=a.get("vmdisk_uuid", ""), bus=bus, index=int(a.get("device_index", -1)),
            size_bytes=int(d.get("size", 0)), ndfs_filepath=a.get("ndfs_filepath", ""),
            container_uuid=d.get("storage_container_uuid"), device_uuid=a.get("device_uuid", ""),
        ))
    return out


def _boot_address(res: dict[str, Any]) -> tuple[DiskBus, int] | None:
    bd = ((res.get("boot_config") or {}).get("boot_device") or {}).get("disk_address") or {}
    if "adapter_type" in bd and "device_index" in bd:
        return _BUS[bd["adapter_type"]], int(bd["device_index"])
    return None


def _guest_os(res: dict[str, Any]) -> GuestOS:
    ver = (((res.get("guest_tools") or {}).get("nutanix_guest_tools") or {}).get("guest_os_version") or "")
    lv = ver.lower()
    if lv.startswith("linux") or any(k in lv for k in ("rhel", "centos", "ubuntu", "suse", "rocky", "alma", "oracle")):
        return GuestOS(OsFamily.LINUX, version=ver or None)
    if "windows" in lv:
        return GuestOS(OsFamily.WINDOWS, version=ver)
    return GuestOS(OsFamily.UNKNOWN)


def to_vmspec(v3: dict[str, Any], v2: dict[str, Any]) -> VmSpec:
    res = _resources(v3)
    meta = v3.get("metadata") or {}
    name = (v3.get("status") or v3.get("spec") or {}).get("name") or v2.get("name") or meta.get("uuid")
    a = disks_v3(v3)
    b = disks_v2(v2)
    a_addr = {(d.bus, d.index): d for d in a}
    b_addr = {(d.bus, d.index): d for d in b}
    if len(a_addr) != len(a) or len(b_addr) != len(b):
        raise NutanixMappingError(f"{name}: duplicate disk addresses in Prism response")
    if set(a_addr) != set(b_addr):
        only3 = sorted(f"{k[0].value}.{k[1]}" for k in set(a_addr) - set(b_addr))
        only2 = sorted(f"{k[0].value}.{k[1]}" for k in set(b_addr) - set(a_addr))
        raise NutanixMappingError(f"{name}: v3/v2 disk sets differ: only in v3 {only3}, only in v2 {only2}")

    boot_addr = _boot_address(res)
    ordered = sorted(a_addr, key=lambda k: (_BUS_RANK[k[0]], k[1]))
    if boot_addr is None:
        boot_addr = ordered[0]
    if boot_addr not in a_addr:
        raise NutanixMappingError(f"{name}: boot device {boot_addr} not among disks")

    disks: list[DiskSpec] = []
    for order, key in enumerate(ordered):
        d3, d2 = a_addr[key], b_addr[key]
        if d3.size_bytes != d2.size_bytes:
            raise NutanixMappingError(f"{name}: size mismatch at {key[0].value}.{key[1]}: v3 {d3.size_bytes} v2 {d2.size_bytes}")
        # v3 disk uuid == v2 device_uuid (the disk device). v2 vmdisk_uuid is the storage vdisk and is
        # legitimately different, so it is never compared with the v3 uuid.
        if d3.uuid and d2.device_uuid and d3.uuid != d2.device_uuid:
            raise NutanixMappingError(f"{name}: disk uuid mismatch at {key[0].value}.{key[1]}: "
                                      f"v3 {d3.uuid} v2 device_uuid {d2.device_uuid}")
        disks.append(DiskSpec(
            id=d2.vmdisk_uuid or d3.uuid, bus=key[0], size_bytes=d3.size_bytes, order=order,
            is_boot=key == boot_addr, bus_index=key[1], controller=_CONTROLLER.get(key[0]),
            source_handle={"vmdisk_uuid": d2.vmdisk_uuid, "ndfs_filepath": d2.ndfs_filepath,
                           "container_uuid": d2.container_uuid or d3.container_uuid, "v3_uuid": d3.uuid,
                           "device_uuid": d2.device_uuid or d3.uuid},
        ))

    nics = []
    for i, n in enumerate(res.get("nic_list") or []):
        ips = [e.get("ip") for e in n.get("ip_endpoint_list") or [] if e.get("ip")]
        sub = n.get("subnet_reference") or {}
        nics.append(NicSpec(id=n.get("uuid") or f"nic{i}", network=sub.get("name") or sub.get("uuid", ""),
                            mac=n.get("mac_address"), ipv4=ips[0] if ips else None,
                            model=(n.get("model") or "virtio").lower()))
    boot_type = (res.get("boot_config") or {}).get("boot_type", "LEGACY")
    return VmSpec(
        name=name,
        source_id=meta.get("uuid") or v2.get("uuid", ""),
        cpu=CpuSpec(sockets=int(res.get("num_sockets", 1)), cores=int(res.get("num_vcpus_per_socket", 1))),
        memory_bytes=int(res.get("memory_size_mib", 0)) << 20,
        disks=disks,
        nics=nics,
        firmware=Firmware.UEFI if boot_type in ("UEFI", "SECURE_BOOT") else Firmware.BIOS,
        secure_boot=boot_type == "SECURE_BOOT",
        guest_os=_guest_os(res),
        machine_type=res.get("machine_type"),
        extra={"nutanix": {"power_state": res.get("power_state"),
                           "cluster": ((v3.get("status") or {}).get("cluster_reference") or {}).get("uuid")}},
    )
