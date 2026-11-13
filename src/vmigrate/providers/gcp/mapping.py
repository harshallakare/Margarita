"""Purpose: PURE mapping from Compute Engine JSON (instance, its disks, machine type) to the IR, and
the two-API disk cross-check. No I/O.

Usage:
    spec = to_vmspec(instance, disks_by_link, machine_type, ignore_local_ssd=False)   # GcpMappingError
    crosscheck(instance, disks_used_by)     # {"gce.instance.disks": n, "gce.disks.users": m}

Rules:
  * every PERSISTENT disk counts, ordered by its index on the instance (boot first)
  * local SSD (type SCRATCH) data is never in snapshots: REFUSED unless ignore_local_ssd=true
  * controller: GCE presents disks over virtio-scsi (interface SCSI) or NVMe; NICs are virtio-net or
    gVNIC. Linux guests on virtio-scsi + virtio-net need no driver injection on KVM targets.
  * UEFI when the boot disk has guestOsFeature UEFI_COMPATIBLE (GCE then boots it with UEFI);
    secure boot from shieldedInstanceConfig; Windows from the WINDOWS feature or a windows license
"""

from __future__ import annotations

from typing import Any

from ...ir import CpuSpec, DiskBus, DiskSpec, Firmware, GuestOS, NicSpec, OsFamily, VmSpec

GIB = 1 << 30
_DISTROS = (("debian", "debian"), ("ubuntu", "ubuntu"), ("rhel", "rhel"), ("centos", "centos"), ("rocky", "rocky"),
            ("alma", "almalinux"), ("sles", "sles"), ("suse", "sles"), ("oracle", "ol"), ("cos-cloud", "cos"),
            ("fedora", "fedora"))


class GcpMappingError(ValueError):
    pass


def last(url: str) -> str:
    return (url or "").rstrip("/").rsplit("/", 1)[-1]


def zone_of(instance: dict[str, Any]) -> str:
    return last(instance.get("zone", ""))


def persistent(instance: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted([d for d in instance.get("disks") or [] if d.get("type", "PERSISTENT") == "PERSISTENT"],
                  key=lambda d: (0 if d.get("boot") else 1, int(d.get("index", 0))))


def crosscheck(instance: dict[str, Any], used_by: list[dict[str, Any]]) -> dict[str, int]:
    return {"gce.instance.disks": len(persistent(instance)), "gce.disks.users": len(used_by)}


def guest_os(boot_disk: dict[str, Any]) -> GuestOS:
    feats = {f.get("type") for f in boot_disk.get("guestOsFeatures") or []}
    lic = " ".join(boot_disk.get("licenses") or []).lower()
    if "WINDOWS" in feats or "windows" in lic:
        return GuestOS(OsFamily.WINDOWS, distro="windows")
    distro = next((d for k, d in _DISTROS if k in lic), None)
    if distro:
        return GuestOS(OsFamily.LINUX, distro=distro)
    return GuestOS(OsFamily.UNKNOWN)


def to_vmspec(instance: dict[str, Any], disks: dict[str, dict[str, Any]], mtype: dict[str, Any], *,
              ignore_local_ssd: bool = False) -> VmSpec:
    name = instance.get("name", "?")
    scratch = [d for d in instance.get("disks") or [] if d.get("type") == "SCRATCH"]
    if scratch and not ignore_local_ssd:
        raise GcpMappingError(f"{name}: {len(scratch)} local SSD(s); snapshots never include them. Move that data to "
                              "persistent disks, or set ignore_local_ssd: true if it only holds scratch data.")
    rows = persistent(instance)
    if not rows:
        raise GcpMappingError(f"{name}: no persistent disks")
    out = []
    for i, a in enumerate(rows):
        d = disks.get(a.get("source", ""))
        where = f"{name}: disk {a.get('deviceName') or last(a.get('source', ''))}"
        if d is None:
            raise GcpMappingError(f"{where} was not returned by the disks API")
        if "/regions/" in a.get("source", ""):
            raise GcpMappingError(f"{where} is a regional disk; snapshot it into a zonal disk first")
        size = int(d.get("sizeGb") or 0) * GIB
        if size <= 0:
            raise GcpMappingError(f"{where}: size unknown")
        nvme = a.get("interface") == "NVME"
        out.append(DiskSpec(
            id=d.get("selfLink") or a["source"], bus=DiskBus.NVME if nvme else DiskBus.SCSI, size_bytes=size, order=i,
            is_boot=bool(a.get("boot")) or i == 0, bus_index=int(a.get("index", i)),
            label=a.get("deviceName") or last(a["source"]), controller="nvme" if nvme else "virtio-scsi",
            source_handle={"name": d.get("name"), "zone": last(d.get("zone", "")), "type": last(d.get("type", "")),
                           "device": a.get("deviceName")},
            shared_id=d.get("selfLink") if a.get("mode") == "READ_ONLY" or len(d.get("users") or []) > 1 else None))
    boot = disks.get(rows[0].get("source", ""), {})
    feats = {f.get("type") for f in boot.get("guestOsFeatures") or []}
    shielded = instance.get("shieldedInstanceConfig") or {}
    uefi = "UEFI_COMPATIBLE" in feats            # GCE boots UEFI-compatible disks with UEFI firmware
    nics = [NicSpec(id=n.get("name", f"nic{k}"), network=f"{last(n.get('network', ''))}/{last(n.get('subnetwork', ''))}",
                    ipv4=n.get("networkIP"), model="gvnic" if n.get("nicType") == "GVNIC" else "virtio")
            for k, n in enumerate(instance.get("networkInterfaces") or [])]
    spec = VmSpec(name=name, source_id=str(instance.get("id") or name),
                  cpu=CpuSpec(sockets=1, cores=max(1, int(mtype.get("guestCpus") or 1))),
                  memory_bytes=int(mtype.get("memoryMb") or 1024) << 20, disks=out, nics=nics,
                  firmware=Firmware.UEFI if uefi else Firmware.BIOS,
                  secure_boot=uefi and bool(shielded.get("enableSecureBoot")), guest_os=guest_os(boot),
                  extra={"gcp": {"zone": zone_of(instance), "machine_type": last(instance.get("machineType", "")),
                                 "status": instance.get("status"), "source_platform": "gcp"}})
    spec.validate()
    return spec
