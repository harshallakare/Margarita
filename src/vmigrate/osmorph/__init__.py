"""Purpose: OSMorphing package. Importing it registers all built-in strategies.

Usage: from vmigrate.osmorph import resolve, MorphRequest; resolve(OsFamily.LINUX, "kvm").run(req, ctx)

Support matrix (status):
  linux   x kvm     : implemented (OS detection, virtio initramfs, network MACs, tool swap, fstab UUID/nofail,
                      tune2fs, SELinux relabel, user offline + first-boot scripts)
  linux   x vmware  : implemented (LSI SAS / PVSCSI + VMXNET3 drivers into the initramfs, open-vm-tools at
                      first boot; VMware tools kept)
  linux   x aws     : prerequisites checked (nvme + ena in the initramfs); disks are not editable in the cloud
  linux   x azure   : prerequisites checked (hv_* drivers, waagent); disks are not editable in the cloud
  windows x kvm     : implemented via virt-v2v-in-place (virtio drivers, tool removal, static IPs,
                      first-boot script via virt-customize); manual_required when the tools are missing
  windows x vmware  : implemented (LSI_SAS made boot-start in the SYSTEM hive via hivexregedit; VMware Tools
                      to install after boot)
  windows x aws     : prerequisites checked (AWS NVMe/ENA drivers, EC2Launch)
  windows x azure   : prerequisites checked (DHCP, RDP; VM Agent recommended)
Cloud cells stop with manual_required until osmorph.cloud_drivers_ready: true.
Every other unregistered cell resolves to ManualStrategy -> "manual OSMorph required".
"""

from . import cloud, linux_kvm, vmware, windows_kvm  # noqa: F401  (registers)
from .base import ManualStrategy, MorphRequest, MorphResult, MorphStep, MorphStrategy, register, registered, resolve

__all__ = ["ManualStrategy", "MorphRequest", "MorphResult", "MorphStep", "MorphStrategy", "register", "registered", "resolve"]
