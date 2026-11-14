"""Purpose: OSMorph for the public-cloud targets (AWS, Azure), where the migrated disks are uploaded straight into
the cloud and are never block-accessible to this worker, so nothing can be changed inside the guest.

These strategies state exactly what the guest needs BEFORE migration and block the run (manual_required) until
the operator confirms it with osmorph.cloud_drivers_ready: true. Nothing is silently assumed.

  linux x aws    : nvme + ena in every kernel's initramfs (Nitro instances), cloud-init or DHCP networking
  windows x aws  : AWS NVMe and ENA drivers installed (AWS PV drivers for Xen types), EC2Launch v2
  linux x azure  : hv_vmbus, hv_storvsc, hv_netvsc in the initramfs, the Azure Linux agent (waagent), DHCP
  windows x azure: (Hyper-V drivers are inbox) the Azure VM Agent recommended, RDP enabled, DHCP

Prepare the source guest, for example on Linux:
  dracut -f --add-drivers "nvme ena"            (RHEL family, for AWS)
  echo -e "nvme\\nena" >> /etc/initramfs-tools/modules && update-initramfs -u     (Debian/Ubuntu)
Usage: resolved by the engine for target platforms "aws" and "azure".
"""

from __future__ import annotations

from ..ir import OsFamily
from .base import MorphRequest, MorphStep, MorphStrategy, register

NEEDS = {
    (OsFamily.LINUX, "aws"): "nvme and ena kernel modules in every kernel's initramfs (Nitro instances boot from NVMe "
                             "and use ENA networking); networking by DHCP or cloud-init",
    (OsFamily.WINDOWS, "aws"): "AWS NVMe and ENA drivers installed in the guest (AWS PV drivers for older Xen instance "
                               "types) and EC2Launch v2; DHCP networking",
    (OsFamily.LINUX, "azure"): "hv_vmbus, hv_storvsc and hv_netvsc in every kernel's initramfs, the Azure Linux agent "
                               "(waagent) installed, DHCP networking",
    (OsFamily.WINDOWS, "azure"): "DHCP networking and RDP enabled (Hyper-V drivers are inbox); the Azure VM Agent "
                                 "recommended",
}


class CloudPrepStrategy(MorphStrategy):
    def __init__(self, family: OsFamily, platform: str):
        self.family, self.platform = family, platform
        self.name = f"{family.value}-{platform}"

    def plan(self, req: MorphRequest) -> list[MorphStep]:
        need = NEEDS[(self.family, self.platform)]
        ready = bool(req.options.get("cloud_drivers_ready"))
        steps = [MorphStep(
            f"{self.platform}-guest-ready",
            f"the disks are uploaded straight into {self.platform.upper()} and cannot be edited here: the guest must "
            f"already have {need}" + (" (confirmed: osmorph.cloud_drivers_ready)" if ready else
                                      "; prepare the source, then set osmorph.cloud_drivers_ready: true"),
            required=not ready, run=None)]
        if self.family == OsFamily.LINUX:
            steps.append(MorphStep("fstab-uuid", "mount filesystems by UUID / LABEL in /etc/fstab (device names change "
                                                 "to /dev/nvme* or /dev/sd*)", required=False, run=None))
        return steps


def cloud_gate(spec: object, platform: str) -> list[str]:
    """Preflight problem for a cloud target when the operator has not confirmed the guest prerequisites, so a
    run stops BEFORE uploading terabytes instead of at the OSMorph step after the copy."""
    extra = getattr(spec, "extra", {}) or {}
    o = extra.get("osmorph") or {}
    fam = getattr(getattr(spec, "guest_os", None), "family", None)
    if o.get("cloud_drivers_ready") or o.get("allow_manual") or (fam, platform) not in NEEDS:
        return []
    return [f"{platform}: the guest must already have {NEEDS[(fam, platform)]} (the disks cannot be edited in "
            f"{platform.upper()}); prepare the source, then set osmorph.cloud_drivers_ready: true"]


for _fam in (OsFamily.LINUX, OsFamily.WINDOWS):
    for _plat in ("aws", "azure"):
        register(_fam, _plat, lambda f=_fam, p=_plat: CloudPrepStrategy(f, p))
