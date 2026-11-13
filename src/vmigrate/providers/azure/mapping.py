"""Purpose: PURE mapping from Azure Resource Manager JSON (VM with instanceView, its managed disks,
NICs and VM size) to the IR, plus the two-API disk cross-check. No I/O.

Usage:
    spec = to_vmspec(vm, disks_by_id, nics, size)    # raises AzureMappingError (fail loud)
    crosscheck(vm, managed_by)                       # {"arm.storageProfile": n, "arm.disks.managedBy": m}

Rules:
  * OS disk first, then data disks by LUN; every managed disk counts, no cap. Cross-checked
    against the subscription's disk list filtered by managedBy == this VM.
  * REFUSED: unmanaged (VHD page-blob) disks, ephemeral OS disks (no managed disk to snapshot),
    disks pending detach, and Confidential VMs (their OS disk is encrypted with a key bound to the
    VM's vTPM, so a copy cannot boot elsewhere).
  * the temporary "resource disk" (D: on Windows, /mnt on Linux) is scratch storage that Azure
    itself wipes; it is not a managed disk and is not copied (recorded in extra).
  * controller: Azure presents disks through Hyper-V storvsc (SCSI) or NVMe on newer sizes, and
    NICs through netvsc / Mellanox VF; none is virtio, so OSMorph injects virtio for KVM targets.
  * Generation 2 (hyperVGeneration V2) -> UEFI; Trusted Launch -> secure boot recorded.
"""

from __future__ import annotations

from typing import Any

from ...ir import CpuSpec, DiskBus, DiskSpec, Firmware, GuestOS, NicSpec, OsFamily, VmSpec

GIB = 1 << 30

_DISTROS = (("canonical", "ubuntu"), ("ubuntu", "ubuntu"), ("redhat", "rhel"), ("rhel", "rhel"),
            ("suse", "sles"), ("sles", "sles"), ("debian", "debian"), ("openlogic", "centos"),
            ("centos", "centos"), ("almalinux", "almalinux"), ("rocky", "rocky"), ("oracle", "ol"))


class AzureMappingError(ValueError):
    pass


def _p(obj: dict[str, Any] | None) -> dict[str, Any]:
    return (obj or {}).get("properties") or {}


def disk_refs(vm: dict[str, Any]) -> list[dict[str, Any]]:
    """[{kind: os|data, lun, name, id (managed disk id or None), raw}] in target order."""
    sp = _p(vm).get("storageProfile") or {}
    os_d = sp.get("osDisk") or {}
    out = [{"kind": "os", "lun": None, "name": os_d.get("name"), "id": (os_d.get("managedDisk") or {}).get("id"),
            "raw": os_d}]
    for d in sorted(sp.get("dataDisks") or [], key=lambda d: int(d.get("lun", 0))):
        out.append({"kind": "data", "lun": int(d.get("lun", 0)), "name": d.get("name"),
                    "id": (d.get("managedDisk") or {}).get("id"), "raw": d})
    return out


def crosscheck(vm: dict[str, Any], managed_by: list[dict[str, Any]]) -> dict[str, int]:
    return {"arm.storageProfile": len(disk_refs(vm)), "arm.disks.managedBy": len(managed_by)}


def guest_os(vm: dict[str, Any], os_disk: dict[str, Any] | None) -> GuestOS:
    p = _p(vm)
    sp = p.get("storageProfile") or {}
    os_type = str((sp.get("osDisk") or {}).get("osType") or _p(os_disk).get("osType") or "").lower()
    iv = p.get("instanceView") or {}
    if os_type == "windows":
        return GuestOS(OsFamily.WINDOWS, distro="windows", version=iv.get("osVersion"))
    if os_type == "linux":
        img = sp.get("imageReference") or {}
        hay = " ".join(str(x).lower() for x in (img.get("publisher"), img.get("offer"), iv.get("osName")) if x)
        distro = next((d for k, d in _DISTROS if k in hay), None)
        return GuestOS(OsFamily.LINUX, distro=distro, version=iv.get("osVersion"))
    return GuestOS(OsFamily.UNKNOWN)


def _mac(m: str | None) -> str | None:
    return m.replace("-", ":").lower() if m else None


def to_vmspec(vm: dict[str, Any], disks: dict[str, dict[str, Any]], nics: list[dict[str, Any]],
              size: dict[str, Any]) -> VmSpec:
    name = vm.get("name") or vm.get("id", "?")
    p = _p(vm)
    sec = p.get("securityProfile") or {}
    if str(sec.get("securityType") or "").lower() == "confidentialvm":
        raise AzureMappingError(f"{name}: Confidential VMs cannot be migrated (the OS disk is encrypted with a key "
                                "bound to the VM's vTPM)")
    by_id = {k.lower(): v for k, v in disks.items()}
    nvme = str((p.get("storageProfile") or {}).get("diskControllerType") or "").lower() == "nvme"
    bus, ctrl = (DiskBus.NVME, "nvme") if nvme else (DiskBus.SCSI, "storvsc")
    out: list[DiskSpec] = []
    for i, ref in enumerate(disk_refs(vm)):
        what = "OS disk" if ref["kind"] == "os" else f"data disk LUN {ref['lun']}"
        where = f"{name}: {what} {ref['name'] or ''}".rstrip()
        raw = ref["raw"]
        if raw.get("vhd"):
            raise AzureMappingError(f"{where} is an unmanaged (VHD page-blob) disk; convert the VM to managed disks first")
        if ref["kind"] == "os" and raw.get("diffDiskSettings"):
            raise AzureMappingError(f"{where} is an ephemeral OS disk: it lives on the host's cache or temp disk and "
                                    "cannot be snapshotted")
        if raw.get("toBeDetached") or raw.get("detachOption"):
            raise AzureMappingError(f"{where} is being detached; wait until the detach finishes")
        if not ref["id"]:
            raise AzureMappingError(f"{where} has no managed disk id")
        d = by_id.get(ref["id"].lower())
        if d is None:
            raise AzureMappingError(f"{where} ({ref['id']}) was not returned by the disks API")
        dp = _p(d)
        size_b = int(dp.get("diskSizeBytes") or 0) or int(dp.get("diskSizeGB") or 0) * GIB
        if size_b <= 0:
            raise AzureMappingError(f"{where}: disk size unknown")
        shares = int(dp.get("maxShares") or 1)
        label = "os" if ref["kind"] == "os" else f"lun{ref['lun']}"
        out.append(DiskSpec(
            id=d.get("id") or ref["id"], bus=bus, size_bytes=size_b, order=i, is_boot=ref["kind"] == "os",
            bus_index=i, label=label, controller=ctrl,
            source_handle={"disk_id": d.get("id") or ref["id"], "name": d.get("name"), "lun": ref["lun"],
                           "sku": (d.get("sku") or {}).get("name"),
                           "encryption": (dp.get("encryption") or {}).get("type")},
            shared_id=(d.get("id") or ref["id"]).lower() if shares > 1 else None))
    nic_rows = []
    for n in nics:
        np_ = _p(n)
        cfgs = np_.get("ipConfigurations") or []
        primary = next((c for c in cfgs if _p(c).get("primary")), cfgs[0] if cfgs else {})
        subnet = (_p(primary).get("subnet") or {}).get("id") or ""
        parts = subnet.split("/")
        net = "/".join(parts[-3:-2] + parts[-1:]) if len(parts) >= 3 else subnet   # "<vnet>/<subnet>"
        nic_rows.append((0 if np_.get("primary") else 1, NicSpec(
            id=n.get("id", ""), network=net, mac=_mac(np_.get("macAddress")),
            ipv4=_p(primary).get("privateIPAddress"),
            model="netvsc" + ("+mlx" if np_.get("enableAcceleratedNetworking") else ""))))
    gen = str(_p(disks.get(out[0].id) or by_id.get(out[0].id.lower())).get("hyperVGeneration")
              or (p.get("instanceView") or {}).get("hyperVGeneration") or "V1").upper()
    uefi = gen == "V2"
    secure = uefi and bool((sec.get("uefiSettings") or {}).get("secureBootEnabled"))
    spec = VmSpec(
        name=name, source_id=vm.get("id", name), cpu=CpuSpec(sockets=1, cores=max(1, int(size.get("numberOfCores") or 1))),
        memory_bytes=int(size.get("memoryInMB") or 1024) << 20, disks=out,
        nics=[n for _, n in sorted(nic_rows, key=lambda r: r[0])],
        firmware=Firmware.UEFI if uefi else Firmware.BIOS, secure_boot=secure,
        guest_os=guest_os(vm, disks.get(out[0].id)),
        extra={"azure": {"vm_size": (p.get("hardwareProfile") or {}).get("vmSize"), "location": vm.get("location"),
                         "generation": gen, "security_type": sec.get("securityType"),
                         "resource_disk_mb": int(size.get("resourceDiskSizeInMB") or 0),
                         "note": "the temporary resource disk is scratch storage and is not copied"
                                 if size.get("resourceDiskSizeInMB") else None,
                         "source_platform": "azure"}})
    spec.validate()
    return spec
