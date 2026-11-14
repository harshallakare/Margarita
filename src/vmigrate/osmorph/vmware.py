"""Purpose: OSMorph strategies for guests landing on VMware vSphere (registered for ("linux", "vmware") and
("windows", "vmware")). They run on the committed target disks, which the VMware target serves over NBD.

Linux: the VMware target presents an LSI Logic SAS (default) or PVSCSI controller and VMXNET3/E1000E NICs.
  Every kernel must have the controller's driver in its initramfs (mptsas / vmw_pvscsi); added with
  update-initramfs or dracut when missing (same machinery as the KVM strategy). open-vm-tools is installed
  at first boot; VMware tools are NOT removed (they are the right tools here), the QEMU agent is left alone.
Windows: Windows ships the LSI SAS driver (lsi_sas) but leaves it disabled unless that hardware was present at
  install time, which ends in INACCESSIBLE_BOOT_DEVICE. The SYSTEM hive is edited offline to make lsi_sas (and
  pvscsi, if the guest has it) boot-start: guestfish downloads the hive, hivexregedit --merge edits it,
  guestfish uploads it back. Needs guestfish + hivexregedit (libguestfs-tools, hivex) on the worker.
  The NIC is E1000E (inbox driver); install VMware Tools afterwards for VMXNET3/PVSCSI.

Usage: resolved by the engine for target platform "vmware"; options as for the KVM strategies.
UNVERIFIED on a real vSphere boot: tested with fakes only.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from ..ir import OsFamily
from . import linux_guest as lg
from .base import MorphRequest, MorphStep, MorphStrategy, register
from .guestfs import Guestfish, _q
from .linux_kvm import LinuxKvmStrategy, inject_drivers

VMWARE_STORAGE = {"lsilogic-sas": ["mptsas", "mptscsih", "mptbase", "scsi_transport_sas"],
                  "pvscsi": ["vmw_pvscsi"], "lsilogic": ["mptspi", "mptscsih", "mptbase"]}
VMWARE_NET = ["vmxnet3", "e1000e"]
SYSTEM_HIVE = "/Windows/System32/config/SYSTEM"
WINDOWS_BOOT_SERVICES = {"lsilogic-sas": ["LSI_SAS"], "pvscsi": ["pvscsi"], "lsilogic": ["LSI_SCSI"]}


def controller_of(req: MorphRequest) -> str:
    return str(req.options.get("vmware_controller") or "lsilogic-sas")


class LinuxVmwareStrategy(LinuxKvmStrategy):
    name = "linux-vmware"
    nic_label = "vmxnet3"
    remove_tools_default = False
    agent_name = "10-open-vm-tools"

    @property
    def platform_label(self) -> str:  # type: ignore[override]
        return "a VMware SCSI controller"

    def needs_drivers(self, req: MorphRequest) -> bool:
        boot = req.spec.boot_disk()
        return (boot.controller or "") != controller_of(req)

    def driver_step(self, gf: Guestfish, can_edit: bool, why: str) -> MorphStep:
        ctl = self._ctl
        mods = VMWARE_STORAGE.get(ctl, VMWARE_STORAGE["lsilogic-sas"]) + VMWARE_NET
        check = (mods[0], mods[0])
        return MorphStep(f"inject-{ctl}-initramfs", f"{why}: {', '.join(mods)} must be in every kernel's initramfs",
                         required=True, run=(lambda: inject_drivers(gf, mods, check, ctl)) if can_edit else None)

    def plan(self, req: MorphRequest) -> list[MorphStep]:
        self._ctl = controller_of(req)
        return super().plan(req)

    def agent_script(self, info: lg.OsInfo) -> str:
        return lg.vmware_tools_script(info)

    def nic_changes(self, req: MorphRequest) -> bool:
        return any(n.model and n.model not in ("vmxnet3",) for n in req.spec.nics)


class WindowsVmwareStrategy(MorphStrategy):
    name = "windows-vmware"

    def __init__(self, which=shutil.which, guestfish_factory=Guestfish):
        self._which, self._gf = which, guestfish_factory

    def plan(self, req: MorphRequest) -> list[MorphStep]:
        ctl = controller_of(req)
        services = WINDOWS_BOOT_SERVICES.get(ctl, ["LSI_SAS"])
        gf = self._gf(req.images, binary=req.options.get("guestfish", "guestfish"))
        can = gf.available() and self._which("hivexregedit") is not None
        boot = req.spec.boot_disk()
        steps = []
        if (boot.controller or "") != ctl:
            steps.append(MorphStep(
                "enable-boot-storage-driver",
                f"make {', '.join(services)} boot-start in the SYSTEM hive (the guest booted from "
                f"{boot.controller or 'another controller'}; otherwise INACCESSIBLE_BOOT_DEVICE)",
                required=True, run=(lambda: enable_boot_services(gf, services)) if can else None))
        steps.append(MorphStep("vmware-tools", "install VMware Tools after the first boot (VMXNET3, PVSCSI, "
                                               "time sync, clean shutdown)", required=False, run=None))
        return steps


def reg_file(services: list[str]) -> str:
    out = ["Windows Registry Editor Version 5.00", ""]
    for cs in ("ControlSet001",):           # the set offline Windows boots with (Select\\Current = 1)
        for svc in services:
            out += [f"[HKEY_LOCAL_MACHINE\\SYSTEM\\{cs}\\Services\\{svc}]", '"Start"=dword:00000000', ""]
    return "\r\n".join(out)


def enable_boot_services(gf: Guestfish, services: list[str]) -> str:
    """Download SYSTEM, merge Start=0 for `services`, upload it back (keeps a .vmigrate-orig copy)."""
    from ..core import cancel
    d = tempfile.mkdtemp(prefix="vmigrate-hive-")
    try:
        hive, reg = Path(d) / "SYSTEM", Path(d) / "boot.reg"
        gf._run(f"download {_q(SYSTEM_HIVE)} {_q(str(hive))}\n", rw=False, inspect=True)
        shutil.copyfile(hive, Path(d) / "SYSTEM.orig")
        reg.write_text(reg_file(services), encoding="utf-8")
        p = cancel.run(["hivexregedit", "--merge", "--prefix", "HKEY_LOCAL_MACHINE\\SYSTEM", str(hive), str(reg)],
                       capture_output=True, text=True, timeout=300)
        if p.returncode != 0:
            raise RuntimeError(f"hivexregedit failed: {p.stderr.strip()[-800:]}")
        gf._run(f"upload {_q(str(Path(d) / 'SYSTEM.orig'))} {_q(SYSTEM_HIVE + '.vmigrate-orig')}\n"
                f"upload {_q(str(hive))} {_q(SYSTEM_HIVE)}\n", rw=True, inspect=True)
        return f"{', '.join(services)} set to boot-start (Start=0)"
    finally:
        shutil.rmtree(d, ignore_errors=True)


register(OsFamily.LINUX, "vmware", LinuxVmwareStrategy)
register(OsFamily.WINDOWS, "vmware", WindowsVmwareStrategy)
