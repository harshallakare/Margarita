"""Purpose: PURE mapping from OCI Core JSON (instance, boot/block volume attachments, volumes, image,
VNICs) to the IR, and the two-query disk cross-check. No I/O.

Usage:
    spec = to_vmspec(instance, boot_atts, vol_atts, volumes, image, vnics)   # raises OciMappingError
    crosscheck(boot_atts, vol_atts, compartment_atts, iid)

Rules:
  * boot volume first, then block volumes in attachment order (device path, then creation time)
  * controller: launch mode PARAVIRTUALIZED / NATIVE (VFIO) / EMULATED and iSCSI attachments are not
    the target's virtio, so the controller is recorded as oci-<type> and OSMorph injects virtio.
    iSCSI-attached volumes are logged in from INSIDE the guest (iscsiadm): after a migration those
    node records and _netdev mounts must be cleaned up (reported by OSMorph as a manual step).
  * firmware from launchOptions.firmware (UEFI_64 -> UEFI); secure boot from platformConfig
  * shareable attachments get a shared_id (multi-attach volumes)
"""

from __future__ import annotations

from typing import Any

from ...ir import CpuSpec, DiskBus, DiskSpec, Firmware, GuestOS, NicSpec, OsFamily, VmSpec

GIB = 1 << 30
_DISTROS = (("oracle", "ol"), ("ubuntu", "ubuntu"), ("centos", "centos"), ("rocky", "rocky"), ("alma", "almalinux"),
            ("red hat", "rhel"), ("rhel", "rhel"), ("debian", "debian"), ("suse", "sles"))


class OciMappingError(ValueError):
    pass


def crosscheck(boot_atts: list[dict[str, Any]], vol_atts: list[dict[str, Any]],
               compartment_atts: list[dict[str, Any]], iid: str) -> dict[str, int]:
    """Attachments filtered by the API (instanceId) vs the compartment-wide list filtered here."""
    return {"oci.attachments(instanceId)": len(boot_atts) + len(vol_atts),
            "oci.attachments(compartment)": len(boot_atts) + sum(1 for a in compartment_atts if a.get("instanceId") == iid)}


def guest_os(image: dict[str, Any] | None) -> GuestOS:
    os_name = str((image or {}).get("operatingSystem") or "").lower()
    ver = (image or {}).get("operatingSystemVersion")
    if "windows" in os_name:
        return GuestOS(OsFamily.WINDOWS, distro="windows", version=ver)
    distro = next((d for k, d in _DISTROS if k in os_name), None)
    if distro or "linux" in os_name:
        return GuestOS(OsFamily.LINUX, distro=distro, version=ver)
    return GuestOS(OsFamily.UNKNOWN)


def _size(v: dict[str, Any]) -> int:
    mb = int(v.get("sizeInMBs") or 0)
    return mb << 20 if mb else int(v.get("sizeInGBs") or 0) * GIB


def to_vmspec(instance: dict[str, Any], boot_atts: list[dict[str, Any]], vol_atts: list[dict[str, Any]],
              volumes: dict[str, dict[str, Any]], image: dict[str, Any] | None, vnics: list[dict[str, Any]]) -> VmSpec:
    name = instance.get("displayName") or instance.get("id", "?")
    if len(boot_atts) != 1:
        raise OciMappingError(f"{name}: expected one attached boot volume, found {len(boot_atts)}")
    lo = instance.get("launchOptions") or {}
    boot_type = str(lo.get("bootVolumeType") or instance.get("launchMode") or "paravirtualized").lower()
    rows = [("boot", boot_atts[0]["bootVolumeId"], boot_type, False)]
    for a in sorted(vol_atts, key=lambda a: (str(a.get("device") or "~"), str(a.get("timeCreated") or ""))):
        if a.get("lifecycleState") != "ATTACHED":
            raise OciMappingError(f"{name}: volume {a.get('volumeId')} is still {a.get('lifecycleState')}")
        rows.append(("block", a["volumeId"], str(a.get("attachmentType") or "paravirtualized").lower(),
                     bool(a.get("isShareable"))))
    disks = []
    for i, (kind, vid, atype, shared) in enumerate(rows):
        v = volumes.get(vid)
        if v is None:
            raise OciMappingError(f"{name}: volume {vid} was not returned")
        size = _size(v)
        if size <= 0:
            raise OciMappingError(f"{name}: volume {vid} size unknown")
        disks.append(DiskSpec(
            id=vid, bus=DiskBus.SCSI, size_bytes=size, order=i, is_boot=kind == "boot", bus_index=i,
            label=("boot" if kind == "boot" else f"vol{i}"), controller=f"oci-{atype}",
            source_handle={"volume_id": vid, "boot": kind == "boot", "attachment": atype,
                           "name": v.get("displayName")},
            shared_id=vid if shared else None))
    sc = instance.get("shapeConfig") or {}
    vcpus = int(sc.get("vcpus") or 0) or int(float(sc.get("ocpus") or 1) * 2)
    mem = int(float(sc.get("memoryInGBs") or 1) * GIB)
    uefi = str(lo.get("firmware") or "").upper().startswith("UEFI")
    nics = [NicSpec(id=n.get("id", ""), network=str(n.get("subnetId") or "").rsplit(".", 1)[-1][:24],
                    mac=(n.get("macAddress") or "").lower() or None, ipv4=n.get("privateIp"),
                    model=f"oci-{str(lo.get('networkType') or 'vfio').lower()}")
            for n in sorted(vnics, key=lambda n: 0 if n.get("isPrimary") else 1)]
    spec = VmSpec(name=name, source_id=instance.get("id", name), cpu=CpuSpec(sockets=1, cores=max(1, vcpus)),
                  memory_bytes=mem, disks=disks, nics=nics, firmware=Firmware.UEFI if uefi else Firmware.BIOS,
                  secure_boot=bool((instance.get("platformConfig") or {}).get("isSecureBootEnabled")),
                  guest_os=guest_os(image),
                  extra={"oci": {"shape": instance.get("shape"), "ad": instance.get("availabilityDomain"),
                                 "state": instance.get("lifecycleState"),
                                 "iscsi_volumes": [r[1] for r in rows if r[2] == "iscsi"], "source_platform": "oci"}})
    spec.validate()
    return spec
