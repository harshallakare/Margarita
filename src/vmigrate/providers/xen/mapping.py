"""Purpose: PURE mapping from XAPI records (VM, VBDs, VDIs, VIFs, networks, guest metrics) to the
IR, the two-method disk cross-check, and the CBT bitmap decoder. No I/O.

Usage:
    spec = to_vmspec(vm, vbds, vdis, vifs, networks, metrics)    # raises XenMappingError
    crosscheck(vbds, vdis_by_vm)              # {"xapi.VM.VBDs": n, "xapi.VDI.VBDs->VM": m}
    bitmap_to_ranges(b64, size)               # VDI.list_changed_blocks -> [ByteRange] (64 KiB blocks)

Rules:
  * every VBD of type Disk counts (CD drives do not); ordered by userdevice; the VBD marked
    bootable is the boot disk (else userdevice 0)
  * disk controller: HVM guests see emulated IDE until the PV drivers load, then xen-blkfront;
    recorded as "xen-blkfront", so OSMorph injects virtio for KVM targets. NICs: xen-netfront.
  * firmware: HVM_boot_params.firmware == "uefi" -> UEFI; platform.secureboot -> secure boot
  * sharable VDIs (clusters) get a shared_id; VDIs on SRs without snapshot support fail at snapshot
"""

from __future__ import annotations

import base64
from typing import Any

from ...core.ranges import ByteRange, normalize
from ...ir import CpuSpec, DiskBus, DiskSpec, Firmware, GuestOS, NicSpec, OsFamily, VmSpec

CBT_BLOCK = 64 << 10
_DISTROS = (("ubuntu", "ubuntu"), ("debian", "debian"), ("red hat", "rhel"), ("rhel", "rhel"), ("centos", "centos"),
            ("rocky", "rocky"), ("alma", "almalinux"), ("suse", "sles"), ("oracle", "ol"), ("fedora", "fedora"))


class XenMappingError(ValueError):
    pass


def disk_vbds(vbds: dict[str, dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    rows = [(ref, v) for ref, v in vbds.items() if v.get("type") == "Disk"]
    return sorted(rows, key=lambda r: int(r[1].get("userdevice") or 0) if str(r[1].get("userdevice", "0")).isdigit() else 99)


def crosscheck(vbds: dict[str, dict[str, Any]], vdis_by_vm: int) -> dict[str, int]:
    return {"xapi.VM.VBDs": len(disk_vbds(vbds)), "xapi.VDI.VBDs->VM": vdis_by_vm}


def guest_os(metrics: dict[str, Any] | None, vm: dict[str, Any]) -> GuestOS:
    osv = (metrics or {}).get("os_version") or {}
    hay = " ".join(str(x).lower() for x in (osv.get("name"), osv.get("distro"), (vm.get("other_config") or {}).get("base_template_name")) if x)
    if "windows" in hay or "microsoft" in hay:
        return GuestOS(OsFamily.WINDOWS, distro="windows")
    distro = next((d for k, d in _DISTROS if k in hay), None)
    if distro or "linux" in hay:
        return GuestOS(OsFamily.LINUX, distro=distro, version=osv.get("major"))
    return GuestOS(OsFamily.UNKNOWN)


def to_vmspec(vm: dict[str, Any], vbds: dict[str, dict[str, Any]], vdis: dict[str, dict[str, Any]],
              vifs: list[dict[str, Any]], networks: dict[str, str], metrics: dict[str, Any] | None) -> VmSpec:
    name = vm.get("name_label") or vm.get("uuid", "?")
    rows = disk_vbds(vbds)
    if not rows:
        raise XenMappingError(f"{name}: no disks")
    boot_ref = next((ref for ref, v in rows if v.get("bootable")), rows[0][0])
    disks = []
    for i, (ref, v) in enumerate(rows):
        vdi = vdis.get(v.get("VDI", ""))
        where = f"{name}: disk {v.get('userdevice')}"
        if vdi is None:
            raise XenMappingError(f"{where} has no VDI (empty drive?)")
        size = int(vdi.get("virtual_size") or 0)
        if size <= 0:
            raise XenMappingError(f"{where}: size unknown")
        disks.append(DiskSpec(
            id=vdi["uuid"], bus=DiskBus.SCSI, size_bytes=size, order=i, is_boot=ref == boot_ref,
            bus_index=int(v.get("userdevice") or i) if str(v.get("userdevice", "")).isdigit() else i,
            label=f"xvd{'abcdefghijklmnopqrstuvwxyz'[i % 26]}", controller="xen-blkfront",
            source_handle={"vdi_uuid": vdi["uuid"], "vdi_ref": v.get("VDI"), "userdevice": v.get("userdevice"),
                           "cbt_enabled": bool(vdi.get("cbt_enabled")), "sr": vdi.get("SR"), "name": vdi.get("name_label")},
            shared_id=vdi["uuid"] if vdi.get("sharable") else None))
    hvm = vm.get("HVM_boot_params") or {}
    platform = vm.get("platform") or {}
    uefi = str(hvm.get("firmware") or "").lower() == "uefi"
    vcpus = int(vm.get("VCPUs_max") or 1)
    cps = int(platform.get("cores-per-socket") or 0) or vcpus
    nics = [NicSpec(id=str(f.get("device")), network=networks.get(f.get("network", ""), ""), mac=f.get("MAC"),
                    model="xen-netfront") for f in sorted(vifs, key=lambda f: int(f.get("device") or 0))]
    spec = VmSpec(name=name, source_id=vm.get("uuid", name), cpu=CpuSpec(sockets=max(1, vcpus // max(1, cps)),
                                                                          cores=max(1, min(cps, vcpus))),
                  memory_bytes=int(vm.get("memory_static_max") or 1 << 30), disks=disks, nics=nics,
                  firmware=Firmware.UEFI if uefi else Firmware.BIOS,
                  secure_boot=uefi and str(platform.get("secureboot", "")).lower() == "true",
                  guest_os=guest_os(metrics, vm),
                  extra={"xen": {"power_state": vm.get("power_state"), "source_platform": "xen"}})
    spec.validate()
    return spec


def bitmap_to_ranges(b64: str, size: int, block: int = CBT_BLOCK) -> list[ByteRange]:
    """VDI.list_changed_blocks returns a base64 bitmap, one bit per 64 KiB block, MSB first."""
    bits = base64.b64decode(b64 or "")
    out = []
    for i, byte in enumerate(bits):
        if not byte:
            continue
        for b in range(8):
            if byte & (0x80 >> b):
                start = (i * 8 + b) * block
                if start < size:
                    out.append(ByteRange(start, min(block, size - start)))
    return normalize(out)
