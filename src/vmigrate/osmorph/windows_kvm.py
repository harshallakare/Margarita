"""Purpose: OSMorph strategy for Windows guests landing on a KVM-family target, done by calling
libguestfs' `virt-v2v-in-place` (GPL; used as an external program, not linked or copied).

Usage: registered for (windows, "kvm"). Needs on the worker:
  * virt-v2v-in-place (virt-v2v >= 2.0), or older `virt-v2v --in-place` (1.4x)
  * the virtio-win drivers: osmorph.virtio_win (ISO or directory), else $VIRTIO_WIN, else
    /usr/share/virtio-win (RHEL `virtio-win` package)

What virt-v2v-in-place does to the committed target disks (offline):
  * injects viostor / vioscsi / netkvm / balloon drivers and registers the boot-critical ones so
    Windows boots from virtio (the step that otherwise ends in INACCESSIBLE_BOOT_DEVICE)
  * removes VMware Tools / Hyper-V integration where it can, schedules qemu-ga install at first boot
  * with osmorph.windows_ip ({mac: {ip, gateway, prefix, dns}}) re-applies static IPs to the new
    NICs on first boot (virt-v2v `--mac MAC:ip:...`; UNVERIFIED on a real Windows guest here)
Then optionally a user first-boot script (osmorph.firstboot_script, .bat/.ps1 content) is added
with `virt-customize --firstboot` (runs once as SYSTEM via the rhsrvany service).

The disks are described to virt-v2v with a generated libvirt domain XML that lists every image in
VM disk order with its format and the SOURCE bus, so the converter sees the multi-disk layout
the guest had.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from ..core import cancel
from ..ir import DiskBus, Firmware, OsFamily
from .base import MorphRequest, MorphStep, MorphStrategy, register
from .linux_kvm import needs_virtio

DEFAULT_VIRTIO_WIN = ("/usr/share/virtio-win/virtio-win.iso", "/usr/share/virtio-win")
_BUS = {DiskBus.SCSI: "scsi", DiskBus.SATA: "sata", DiskBus.IDE: "ide", DiskBus.VIRTIO: "virtio",
        DiskBus.NVME: "sata"}  # libvirt nvme disks are too new to rely on; the bus only names drives here


class V2vError(RuntimeError):
    pass


def domain_xml(req: MorphRequest) -> str:
    """Minimal libvirt XML describing the guest as it was on the source (disk order + bus)."""
    s = req.spec
    disks = []
    letters = "abcdefghijklmnopqrstuvwxyz"
    ordered = s.ordered_disks()
    for i, ((img, fmt), d) in enumerate(zip(req.images, ordered, strict=False)):
        bus = _BUS.get(d.bus, "scsi")
        pfx = "vd" if bus == "virtio" else ("hd" if bus == "ide" else "sd")
        dev = pfx + (letters[i] if i < 26 else letters[i // 26 - 1] + letters[i % 26])
        src = str(img)
        if src.startswith("nbd://?socket="):     # VHI helper_vm mode: the volume is served over NBD
            disks.append(f"""    <disk type='network' device='disk'>
      <driver name='qemu' type={quoteattr(fmt)}/>
      <source protocol='nbd'>
        <host transport='unix' socket={quoteattr(src.split("socket=", 1)[1])}/>
      </source>
      <target dev='{dev}' bus='{bus}'/>
    </disk>""")
            continue
        disks.append(f"""    <disk type='file' device='disk'>
      <driver name='qemu' type={quoteattr(fmt)}/>
      <source file={quoteattr(src)}/>
      <target dev='{dev}' bus='{bus}'/>
    </disk>""")
    loader = "\n    <loader readonly='yes' type='pflash'>/usr/share/OVMF/OVMF_CODE.fd</loader>" \
        if s.firmware == Firmware.UEFI else ""
    mem_kib = max(s.memory_bytes // 1024, 1)
    return f"""<domain type='kvm'>
  <name>{escape(s.name)}</name>
  <memory unit='KiB'>{mem_kib}</memory>
  <vcpu>{max(s.cpu.total, 1)}</vcpu>
  <os>
    <type arch='x86_64'>hvm</type>{loader}
  </os>
  <devices>
{chr(10).join(disks)}
  </devices>
</domain>
"""


def mac_args(windows_ip: dict[str, dict[str, Any]]) -> list[str]:
    """{mac: {ip, gateway, prefix, dns}} -> ['--mac', 'MAC:ip:IP,GW,PREFIX,DNS1,DNS2', ...]."""
    out = []
    for mac, cfg in (windows_ip or {}).items():
        ip = cfg.get("ip")
        if not ip:
            continue
        parts = [ip, cfg.get("gateway") or "", str(cfg.get("prefix") or "")] + list(cfg.get("dns") or [])
        out += ["--mac", f"{mac.lower()}:ip:{','.join(parts).rstrip(',')}"]
    return out


class Tools:
    """Locates the converter and drivers; injectable for tests."""

    def __init__(self, which=shutil.which, exists=os.path.exists, env=None, run=cancel.run):
        self.which, self.exists, self.env, self._run = which, exists, (os.environ if env is None else env), run

    def converter(self) -> list[str] | None:
        if self.which("virt-v2v-in-place"):
            return ["virt-v2v-in-place"]
        if self.which("virt-v2v"):
            return ["virt-v2v", "--in-place"]           # virt-v2v 1.4x
        return None

    def virtio_win(self, opt: str | None) -> str | None:
        for c in (opt, self.env.get("VIRTIO_WIN"), *DEFAULT_VIRTIO_WIN):
            if c and self.exists(c):
                return c
        return None

    def customize(self) -> str | None:
        return "virt-customize" if self.which("virt-customize") else None

    def run(self, argv: list[str], env_extra: dict[str, str], timeout: int) -> str:
        p = self._run(argv, capture_output=True, text=True, timeout=timeout, env={**self.env, **env_extra})
        if p.returncode != 0:
            tail = (p.stderr or p.stdout or "").strip()[-3000:]
            raise V2vError(f"{argv[0]} failed ({p.returncode}): {tail}")
        return p.stdout


class WindowsKvmStrategy(MorphStrategy):
    name = "windows-kvm (virt-v2v-in-place)"

    def __init__(self, tools: Tools | None = None):
        self.tools = tools or Tools()

    def plan(self, req: MorphRequest) -> list[MorphStep]:
        opts = req.options
        boot = req.spec.boot_disk()
        if not needs_virtio(boot.controller, boot.bus, req.target_boot_bus) and not opts.get("force_v2v"):
            # e.g. Nutanix AHV -> VHI: both KVM, the guest already boots from virtio-scsi on the same
            # bus, so its installed virtio drivers (Nutanix VirtIO) keep working; nothing to inject.
            why = (f"not needed: the guest already boots from {boot.controller} ({boot.bus.value}) and the target "
                   f"presents the same virtio controller, so its virtio drivers are already installed")
            steps = [MorphStep("virtio-drivers", why, required=False, run=lambda: why)]
            return steps + self._extra_steps(req)
        conv = self.tools.converter()
        drivers = self.tools.virtio_win(opts.get("virtio_win"))
        images_ok = bool(req.images)
        missing = [m for m, ok in (("virt-v2v-in-place", conv), ("virtio-win drivers", drivers),
                                   ("committed disk images", images_ok)) if not ok]
        steps = [MorphStep(
            "virtio-drivers",
            "virt-v2v-in-place: inject viostor/vioscsi/netkvm, register boot drivers, remove VMware/Hyper-V tools,"
            " qemu-ga at first boot" + (f" — missing here: {', '.join(missing)}" if missing else ""),
            required=True,
            run=(lambda: self._convert(req, conv, drivers)) if not missing else None)]  # type: ignore[arg-type]
        if opts.get("windows_ip"):
            steps[0].description += f"; static IPs for {len(opts['windows_ip'])} NIC(s)"
        return steps + self._extra_steps(req, v2v=True)

    def _extra_steps(self, req: MorphRequest, *, v2v: bool = False) -> list[MorphStep]:
        opts, steps, images_ok = req.options, [], bool(req.images)
        if not (v2v and opts.get("windows_ip")) and any(n.ipv4 for n in req.spec.nics):
            steps.append(MorphStep(
                "windows-static-ip",
                "the guest has fixed IPs and the NIC changes to virtio-net: Windows will see a new adapter; "
                "set osmorph.windows_ip {mac: {ip, gateway, prefix, dns}} to re-apply them, or fix by console",
                required=False, run=None))
        fb = str(opts.get("firstboot_script") or "")
        if fb.strip():
            cust = self.tools.customize()
            steps.append(MorphStep(
                "firstboot-script", "virt-customize --firstboot: run the operator's script once as SYSTEM",
                required=True, run=(lambda: self._firstboot(req, cust, fb)) if cust and images_ok else None))
        if str(opts.get("offline_script") or "").strip():
            steps.append(MorphStep("offline-script", "offline scripts are Linux-only; use a first-boot script on "
                                   "Windows", required=True, run=None))
        return steps

    def _convert(self, req: MorphRequest, conv: list[str], drivers: str) -> str:
        with tempfile.TemporaryDirectory(prefix="vmigrate-v2v-") as td:
            xml = Path(td) / "domain.xml"
            xml.write_text(domain_xml(req))
            argv = [*conv, "-i", "libvirtxml", str(xml), *mac_args(req.options.get("windows_ip") or {})]
            if req.options.get("v2v_args"):
                argv += [str(a) for a in req.options["v2v_args"]]
            out = self.tools.run(argv, {"VIRTIO_WIN": drivers, "LIBGUESTFS_BACKEND": "direct"},
                                 int(req.options.get("v2v_timeout", 7200)))
        last = [ln for ln in out.splitlines() if ln.strip()][-1:] or ["ok"]
        return f"{' '.join(conv)} converted {len(req.images)} disk(s) with drivers from {drivers}: {last[0][:200]}"

    def _firstboot(self, req: MorphRequest, cust: str, script: str) -> str:
        ext = ".ps1" if req.options.get("firstboot_shell") == "powershell" else ".bat"
        with tempfile.TemporaryDirectory(prefix="vmigrate-fb-") as td:
            f = Path(td) / f"vmigrate-firstboot{ext}"
            f.write_text(script.replace("\r\n", "\n").replace("\n", "\r\n"))
            argv = [cust]
            for img, fmt in req.images:
                argv += ["--format", fmt, "-a", str(img)]
            argv += ["--firstboot", str(f)]
            self.tools.run(argv, {"LIBGUESTFS_BACKEND": "direct"}, 1800)
        return f"first-boot script installed ({ext})"


register(OsFamily.WINDOWS, "kvm", WindowsKvmStrategy)
