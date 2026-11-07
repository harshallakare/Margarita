"""Purpose: OSMorphing package. Importing it registers all built-in strategies.

Usage: from vmigrate.osmorph import resolve, MorphRequest; resolve(OsFamily.LINUX, "kvm").run(req, ctx)

Support matrix (status):
  linux   x kvm     : implemented (bus-match, fstab nofail, tune2fs); virtio injection = manual
  linux   x vmware  : planned  (pvscsi/vmxnet3, open-vm-tools)
  linux   x aws     : planned  (ena/nvme, cloud-init, serial console)
  linux   x azure   : planned  (hv_*, waagent)
  windows x kvm     : planned  (DISM viostor/vioscsi/netkvm + CriticalDeviceDatabase, cloudbase-init)
  windows x vmware  : planned  (pvscsi/vmxnet3 + VMware Tools)
  windows x aws     : planned  (ENA/NVMe/PV + EC2Launch)
  windows x azure   : planned  (Azure VM Agent + hv drivers)
Every planned cell resolves to ManualStrategy -> "manual OSMorph required".
"""

from . import linux_kvm  # noqa: F401  (registers)
from .base import ManualStrategy, MorphRequest, MorphResult, MorphStep, MorphStrategy, register, registered, resolve

__all__ = ["ManualStrategy", "MorphRequest", "MorphResult", "MorphStep", "MorphStrategy", "register", "registered", "resolve"]
