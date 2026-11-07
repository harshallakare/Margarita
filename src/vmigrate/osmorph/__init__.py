"""Purpose: OSMorphing package. Importing it registers all built-in strategies.

Usage: from vmigrate.osmorph import resolve, MorphRequest; resolve(OsFamily.LINUX, "kvm").run(req, ctx)

Support matrix (status):
  linux   x kvm     : implemented (OS detection, virtio initramfs, network MACs, tool swap, fstab UUID/nofail,
                      tune2fs, SELinux relabel, user offline + first-boot scripts)
  linux   x vmware  : planned  (pvscsi/vmxnet3, open-vm-tools)
  linux   x aws     : planned  (ena/nvme, cloud-init, serial console)
  linux   x azure   : planned  (hv_*, waagent)
  windows x kvm     : implemented via virt-v2v-in-place (virtio drivers, tool removal, static IPs,
                      first-boot script via virt-customize); manual_required when the tools are missing
  windows x vmware  : planned  (pvscsi/vmxnet3 + VMware Tools)
  windows x aws     : planned  (ENA/NVMe/PV + EC2Launch)
  windows x azure   : planned  (Azure VM Agent + hv drivers)
Every planned cell resolves to ManualStrategy -> "manual OSMorph required".
"""

from . import linux_kvm, windows_kvm  # noqa: F401  (registers)
from .base import ManualStrategy, MorphRequest, MorphResult, MorphStep, MorphStrategy, register, registered, resolve

__all__ = ["ManualStrategy", "MorphRequest", "MorphResult", "MorphStep", "MorphStrategy", "register", "registered", "resolve"]
