"""Purpose: PURE mapping from oVirt REST JSON (VM, disk attachments, disks, NICs) to the IR, plus
the two-API disk cross-check and checkpoint-chain helpers. No I/O.

Usage:
    spec = to_vmspec(vm, attachments, disks_by_id, nics, networks)   # raises OvirtMappingError
    crosscheck(attachments, disks_by_search)    # {"ovirt.diskattachments": n, "ovirt.disks?search": m}
    leaf_checkpoint(checkpoints); root_checkpoint(checkpoints)

Rules:
  * every ACTIVE disk attachment counts; inactive (deactivated) attachments are REFUSED: they are
    part of the VM but the guest does not see them, so the operator must decide
  * direct LUN disks are REFUSED (not covered by backups); managed block storage (cinderlib)
    disks are REFUSED (no ImageIO backup support)
  * controller: virtio -> virtio-blk, virtio_scsi -> virtio-scsi, sata -> ahci, ide -> ide
  * firmware from bios.type (q35_ovmf / q35_secure_boot -> UEFI); guest OS from os.type
  * the oVirt API returns numbers as strings; everything is converted here
"""

from __future__ import annotations

from typing import Any

from ...ir import CpuSpec, DiskBus, DiskSpec, Firmware, GuestOS, NicSpec, OsFamily, VmSpec

_IFACE = {"virtio": (DiskBus.VIRTIO, "virtio-blk"), "virtio_scsi": (DiskBus.SCSI, "virtio-scsi"),
          "sata": (DiskBus.SATA, "ahci"), "ide": (DiskBus.IDE, "ide"), "spapr_vscsi": (DiskBus.SCSI, "spapr-vscsi")}
_DISTROS = (("rhel", "rhel"), ("centos", "centos"), ("ubuntu", "ubuntu"), ("debian", "debian"), ("sles", "sles"),
            ("suse", "sles"), ("oracle", "ol"), ("rocky", "rocky"), ("alma", "almalinux"), ("fedora", "fedora"))


class OvirtMappingError(ValueError):
    pass


def _b(v: Any) -> bool:
    return str(v).lower() == "true"


def _i(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def crosscheck(attachments: list[dict[str, Any]], disks_by_search: list[dict[str, Any]]) -> dict[str, int]:
    return {"ovirt.diskattachments": len(attachments), "ovirt.disks?search=vm_names": len(disks_by_search)}


def guest_os(vm: dict[str, Any]) -> GuestOS:
    t = str((vm.get("os") or {}).get("type") or "").lower()
    if t.startswith("windows"):
        return GuestOS(OsFamily.WINDOWS, distro="windows", version=t.removeprefix("windows_").removesuffix("x64") or None)
    if t in ("", "other"):
        return GuestOS(OsFamily.UNKNOWN)
    distro = next((d for k, d in _DISTROS if k in t), None)
    return GuestOS(OsFamily.LINUX, distro=distro)


def to_vmspec(vm: dict[str, Any], attachments: list[dict[str, Any]], disks: dict[str, dict[str, Any]],
              nics: list[dict[str, Any]], networks: dict[str, str]) -> VmSpec:
    name = vm.get("name") or vm.get("id", "?")
    if not attachments:
        raise OvirtMappingError(f"{name}: no disks")
    rows = []
    for a in attachments:
        did = (a.get("disk") or {}).get("id") or a.get("id")
        d = disks.get(did)
        if d is None:
            raise OvirtMappingError(f"{name}: disk {did} was not returned by /disks")
        where = f"{name}: disk {d.get('alias') or d.get('name') or did}"
        if not _b(a.get("active", "true")):
            raise OvirtMappingError(f"{where} is attached but deactivated; activate or detach it first")
        st = str(d.get("storage_type") or "image")
        if st == "lun":
            raise OvirtMappingError(f"{where} is a direct LUN; oVirt backups do not cover it")
        if st != "image":
            raise OvirtMappingError(f"{where} is on {st} storage, which oVirt cannot back up through ImageIO")
        iface = str(a.get("interface") or "virtio_scsi")
        if iface not in _IFACE:
            raise OvirtMappingError(f"{where}: unknown disk interface {iface!r}")
        size = _i(d.get("provisioned_size"))
        if size <= 0:
            raise OvirtMappingError(f"{where}: size unknown")
        rows.append((0 if _b(a.get("bootable")) else 1, a, d, iface, size))
    rows.sort(key=lambda r: r[0])                 # bootable first, otherwise the API's (attach) order
    out = []
    for i, (_, a, d, iface, size) in enumerate(rows):
        bus, ctrl = _IFACE[iface]
        out.append(DiskSpec(
            id=d["id"], bus=bus, size_bytes=size, order=i, is_boot=i == 0, bus_index=i,
            label=str(d.get("alias") or d.get("name") or d["id"])[:40], controller=ctrl,
            source_handle={"disk_id": d["id"], "format": d.get("format"), "backup": d.get("backup"),
                           "logical_name": a.get("logical_name")},
            shared_id=d["id"] if _b(d.get("shareable")) else None))
    topo = (vm.get("cpu") or {}).get("topology") or {}
    sockets = max(1, _i(topo.get("sockets"), 1))
    cores = max(1, _i(topo.get("cores"), 1)) * max(1, _i(topo.get("threads"), 1))
    bios = str((vm.get("bios") or {}).get("type") or "").lower()
    uefi = "ovmf" in bios or "secure_boot" in bios
    nic_specs = []
    for n in nics:
        pid = (n.get("vnic_profile") or {}).get("id")
        nic_specs.append(NicSpec(id=n.get("id", ""), network=networks.get(pid or "", "") if pid else "",
                                 mac=(n.get("mac") or {}).get("address"), model=n.get("interface")))
    spec = VmSpec(name=name, source_id=vm.get("id", name), cpu=CpuSpec(sockets=sockets, cores=cores),
                  memory_bytes=_i(vm.get("memory"), 1 << 30), disks=out, nics=nic_specs,
                  firmware=Firmware.UEFI if uefi else Firmware.BIOS, secure_boot="secure_boot" in bios,
                  guest_os=guest_os(vm),
                  extra={"ovirt": {"status": vm.get("status"), "bios": bios, "os_type": (vm.get("os") or {}).get("type"),
                                   "source_platform": "kvm"}})
    spec.validate()
    return spec


def leaf_checkpoint(checkpoints: list[dict[str, Any]]) -> str | None:
    """The newest checkpoint of the chain (no other checkpoint names it as parent)."""
    parents = {c.get("parent_id") for c in checkpoints if c.get("parent_id")}
    leaves = [c for c in checkpoints if c.get("id") not in parents]
    if not leaves:
        return None
    return max(leaves, key=lambda c: str(c.get("creation_date") or ""))["id"]


def root_checkpoint(checkpoints: list[dict[str, Any]]) -> str | None:
    ids = {c.get("id") for c in checkpoints}
    roots = [c for c in checkpoints if not c.get("parent_id") or c.get("parent_id") not in ids]
    return roots[0]["id"] if roots else None
