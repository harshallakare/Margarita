"""Purpose: OSMorph strategy for Linux guests landing on a KVM-family target (VHI/OpenStack,
Proxmox, Nutanix AHV).

Usage: registered automatically for (linux, "kvm"); the engine resolves and runs it.

Decision logic:
  * source controller is virtio (AHV virtio-scsi / virtio-blk) AND the bus is unchanged
    (AHV scsi -> VHI hw_disk_bus=scsi): NO driver injection needed (proven on Nutanix -> VHI).
  * otherwise — the guest booted from pvscsi/lsilogic/ahci/ide (VMware, Hyper-V, ...), the
    controller is unknown, or the bus changes — virtio drivers MUST be in the initramfs. That is a
    REQUIRED step: automated offline with guestfish when available (per kernel: skip if virtio is
    built in or already in the initramfs; else dracut --add-drivers (RHEL/SUSE family) or
    initramfs-tools modules + update-initramfs (Debian/Ubuntu); then verify). Without guestfish
    -> manual_required (never a silent bad boot).
  * NICs that were not virtio (vmxnet3, e1000): interface names/MACs change -> note.
  * optional hardening (pitfall 9): data mounts get `nofail` + fs_passno 0, and periodic
    fsck is disabled on ext* filesystems via tune2fs -c 0 -i 0.
  * OS detection (os-release / redhat-release / SuSE-release / debian_version) is recorded.
  * network config is made hardware-independent (see linux_guest.py): MACs rewritten when
    known (osmorph.mac_map, or the target keeps source MACs), otherwise unpinned; udev
    persistent-net rules removed; osmorph.net_rename renames interfaces.
  * VMware / Hyper-V / Azure guest tools are removed (falls back to disabling their units) and
    qemu-guest-agent is installed on first boot.
  * fstab /dev/sdX-style entries become UUID= using the filesystems' real UUIDs.
  * user scripts: osmorph.scripts [{name, when: offline|firstboot, script}] — offline scripts run
    inside the guest image now (a failure stops the migration); first-boot scripts run once on
    the target through a self-disabling systemd unit (log: /var/log/vmigrate-firstboot.log).
  * SELinux guests get /.autorelabel because files were edited offline.
Options (from config osmorph.*): fstab_harden (default True), disable_periodic_fsck (True),
fstab_uuid_map ({"/dev/sdb1": "uuid"}, merged over the detected UUIDs), guestfish (binary path),
network (True), mac_map, net_rename, remove_hypervisor_tools (True), install_guest_agent (True),
scripts / offline_script / firstboot_script, selinux_relabel (True).
"""

from __future__ import annotations

from ..ir import DiskBus, OsFamily
from .base import MorphRequest, MorphStep, MorphStrategy, register
from . import linux_guest as lg
from .fstab import harden_fstab
from .guestfs import Guestfish

_VIRTIO_FAMILY = {DiskBus.SCSI, DiskBus.VIRTIO}
KVM_NATIVE_CONTROLLERS = {"virtio-scsi", "virtio-blk"}
VIRTIO_DRIVERS = ["virtio_blk", "virtio_scsi", "virtio_pci", "virtio_net", "virtio_console"]
_DEBIAN_LIKE = {"debian", "ubuntu", "linuxmint", "pop", "raspbian", "kali"}


def _os_release(text: str) -> set[str]:
    ids: set[str] = set()
    for line in text.splitlines():
        if line.startswith(("ID=", "ID_LIKE=")):
            ids.update(line.split("=", 1)[1].strip().strip('"').lower().split())
    return ids


def needs_virtio(src_controller: str | None, src_bus: DiskBus, target_bus: DiskBus) -> bool:
    return src_controller not in KVM_NATIVE_CONTROLLERS or src_bus != target_bus


def inject_virtio(gf: Guestfish) -> str:
    """Make sure every installed kernel can boot from virtio. Raises on failure (required step)."""
    return inject_drivers(gf, VIRTIO_DRIVERS, ("virtio_scsi", "virtio_blk", "virtio_pci"), "virtio")


def inject_drivers(gf: Guestfish, drivers: list[str], check: tuple[str, ...], label: str) -> str:
    """Make sure every installed kernel has `check` modules (built in or in its initramfs); otherwise add
    `drivers` and rebuild the initramfs. Raises on failure (required step)."""
    ids = _os_release(gf.cat("/etc/os-release")) if gf.exists("/etc/os-release") else set()
    kvers = sorted(f[len("vmlinuz-"):] for f in gf.ls("/boot") if f.startswith("vmlinuz-") and "rescue" not in f)
    if not kvers:
        raise RuntimeError("no kernels found in /boot")
    debian = bool(ids & _DEBIAN_LIKE)
    done: list[str] = []

    def present(k: str) -> bool:
        builtin = f"/lib/modules/{k}/modules.builtin"
        if gf.exists(builtin) and all(f"/{m}.ko" in gf.cat(builtin) for m in check):
            return True
        lister = f"lsinitramfs /boot/initrd.img-{k}" if debian else f"lsinitrd --kver {k}"
        try:
            out = gf.command(lister, rw=False)
        except Exception:  # noqa: BLE001 - no initramfs yet / tool missing -> rebuild
            return False
        return all(m in out for m in check[:2])

    todo = [k for k in kvers if not present(k)]
    if not todo:
        return f"{label} already available for kernels {kvers}"
    if debian:
        mods = gf.cat("/etc/initramfs-tools/modules") if gf.exists("/etc/initramfs-tools/modules") else ""
        add = [m for m in drivers if m not in mods.split()]
        if add:
            gf.upload_text(mods.rstrip("\n") + "\n" + "\n".join(add) + "\n", "/etc/initramfs-tools/modules")
        for k in todo:
            gf.command(f"update-initramfs -u -k {k}")
    else:
        for k in todo:
            gf.command(f"dracut -f --kver {k} --add-drivers '{' '.join(drivers)}'")
    still = [k for k in todo if not present(k)]
    if still:
        raise RuntimeError(f"{label} drivers still missing from the initramfs of {still} after rebuild")
    done = todo
    return f"{label} added to initramfs of {done} ({'update-initramfs' if debian else 'dracut'})"


class LinuxKvmStrategy(MorphStrategy):
    name = "linux-kvm"
    # what the target presents; subclasses (VMware) override these four
    platform_label = "virtio"
    nic_label = "virtio-net"

    def needs_drivers(self, req: MorphRequest) -> bool:
        boot = req.spec.boot_disk()
        return needs_virtio(boot.controller, boot.bus, req.target_boot_bus)

    def driver_step(self, gf: Guestfish, can_edit: bool, why: str) -> MorphStep:
        return MorphStep("inject-virtio-initramfs",
                         f"{why}: virtio_blk/virtio_scsi/virtio_pci/virtio_net must be in every kernel's initramfs",
                         required=True, run=(lambda: inject_virtio(gf)) if can_edit else None)

    remove_tools_default = True                # VMware/Hyper-V/Azure tools are useless on KVM
    keep_tool_families: tuple[str, ...] = ()   # tool families the target still needs (Azure: hyperv, azure)
    agent_name = "10-qemu-guest-agent"

    def agent_script(self, info: lg.OsInfo) -> str:
        return lg.guest_agent_script(info)

    def nic_changes(self, req: MorphRequest) -> bool:
        return any(n.model and not n.model.startswith("virtio") for n in req.spec.nics)

    def __init__(self, guestfish_factory=Guestfish):
        self._gf_factory = guestfish_factory

    def plan(self, req: MorphRequest) -> list[MorphStep]:
        steps: list[MorphStep] = []
        opts = req.options
        src_bus = req.spec.boot_disk().bus
        gf = self._gf_factory(req.images, binary=opts.get("guestfish", "guestfish"))
        can_edit = gf.available()

        self._info = lg.OsInfo()
        steps.append(MorphStep("detect-os", "identify distribution and package manager", required=False,
                               run=(lambda: self._detect(gf)) if can_edit else None))

        boot = req.spec.boot_disk()
        if self.needs_drivers(req):
            why = (f"boot bus changes {src_bus.value} -> {req.target_boot_bus.value}" if src_bus != req.target_boot_bus
                   else f"guest booted from a {boot.controller or 'unknown'} controller, the target presents "
                        f"{self.platform_label}")
            steps.append(self.driver_step(gf, can_edit, why))
        if opts.get("network", True):
            src_macs = [n.mac for n in req.spec.nics if n.mac]
            mac_map = dict(opts.get("mac_map") or {})
            if opts.get("preserve_mac"):
                mac_map = {m: m for m in src_macs} | mac_map
            what = ("rewrite MACs to the target's" if mac_map else "remove MAC pinning") + \
                " in ifcfg / NetworkManager / netplan; drop udev persistent-net rules"
            steps.append(MorphStep("network-config", what, required=False,
                                   run=(lambda: self._network(gf, mac_map, opts.get("net_rename") or {}))
                                   if can_edit else None))
        if self.nic_changes(req):
            models = sorted({n.model for n in req.spec.nics if n.model})
            steps.append(MorphStep(
                "network-naming",
                f"NIC model changes ({', '.join(models)} -> {self.nic_label}): interface names may change; check the "
                "network-config step above, or use DHCP/cloud-init, or set osmorph.net_rename",
                required=False, run=None))

        if opts.get("remove_hypervisor_tools", self.remove_tools_default):
            steps.append(MorphStep("remove-hypervisor-tools",
                                   "remove VMware / Hyper-V / Azure guest tools (or disable their services)",
                                   required=False, run=(lambda: self._remove_tools(gf)) if can_edit else None))

        if opts.get("fstab_harden", True):
            def _fstab() -> str:
                old = gf.cat("/etc/fstab")
                uuids = _devname_aliases(gf.fs_uuids()) | dict(opts.get("fstab_uuid_map") or {})
                new, changes = harden_fstab(old, device_uuids=uuids)
                if not changes:
                    return "fstab already hardened"
                gf.upload_text(old, "/etc/fstab.vmigrate-orig")
                gf.upload_text(new, "/etc/fstab")
                return "; ".join(changes)

            steps.append(MorphStep(
                "fstab-nofail",
                "data mounts: add nofail, set fs_passno=0, /dev/sdX -> UUID= (avoid hours-long boot fsck / "
                "emergency mode / renamed devices)",
                required=False,
                run=_fstab if can_edit else None,
            ))

        if opts.get("disable_periodic_fsck", True):
            def _tune() -> str:
                devs = [d for d, fs in gf.list_filesystems().items() if fs in ("ext2", "ext3", "ext4")]
                gf.tune2fs_disable_periodic_fsck(devs)
                return f"tune2fs -c 0 -i 0 on {devs}" if devs else "no ext* filesystems"

            steps.append(MorphStep(
                "tune2fs-no-periodic-fsck",
                "tune2fs -c 0 -i 0 on ext* filesystems; then run `fsck -n` once offline "
                "(extent-tree 'IGNORED' lines are not errors)",
                required=False,
                run=_tune if can_edit else None,
            ))

        offline, firstboot = lg.user_scripts(opts)
        for name, body in offline:
            steps.append(MorphStep(f"offline-script:{name}", "run the operator's script inside the guest image now",
                                   required=True,
                                   run=(lambda b=body: (gf.run_script(b).strip()[-500:] or "ok")) if can_edit else None))
        fb = list(firstboot)
        if opts.get("install_guest_agent", True):
            fb.insert(0, (self.agent_name, self.agent_script(self._info)))
        if fb:
            steps.append(MorphStep(
                "firstboot-scripts",
                f"install {len(fb)} first-boot script(s) ({', '.join(n for n, _ in fb)}) run once by "
                "vmigrate-firstboot.service; log /var/log/vmigrate-firstboot.log",
                required=bool(firstboot),
                run=(lambda: self._firstboot(gf, fb)) if can_edit else None))

        if opts.get("selinux_relabel", True):
            steps.append(MorphStep("selinux-relabel", "SELinux guests: touch /.autorelabel (files were edited offline)",
                                   required=False, run=(lambda: self._relabel(gf)) if can_edit else None))
        return steps


    # ------------------------------------------------------------------ step bodies
    def _detect(self, gf: Guestfish) -> str:
        files = {p: gf.cat(p) for p in ("/etc/os-release", "/etc/redhat-release", "/etc/SuSE-release",
                                         "/etc/debian_version") if gf.exists(p)}
        self._info = lg.detect_os(files)
        i = self._info
        return f"{i.pretty or i.distro} (family {i.family}, version {i.version or '?'})"

    def _network(self, gf: Guestfish, mac_map: dict, rename: dict) -> str:
        changed: list[str] = []

        def edit(path: str, fn) -> None:
            old = gf.cat(path)
            new, ch = fn(old, mac_map=mac_map, rename=rename)
            if ch:
                gf.upload_text(old, f"{path}.vmigrate-orig")
                gf.upload_text(new, path)
                changed.append(f"{path}: {', '.join(ch)}")

        for d, pat, fn in (("/etc/sysconfig/network-scripts", "ifcfg-", lg.unpin_ifcfg),
                           ("/etc/sysconfig/network", "ifcfg-", lg.unpin_ifcfg),
                           ("/etc/NetworkManager/system-connections", ".nmconnection", lg.unpin_nmconnection),
                           ("/etc/netplan", ".yaml", lg.unpin_netplan)):
            if not gf.is_dir(d):
                continue
            for f in gf.ls(d):
                if (f.startswith(pat) if pat.startswith("ifcfg") else f.endswith(pat)) and f != "ifcfg-lo" \
                        and not f.endswith(".vmigrate-orig"):
                    edit(f"{d}/{f}", fn)
        if gf.is_dir("/etc/udev/rules.d"):
            for p in lg.udev_net_rules_to_remove([f"/etc/udev/rules.d/{f}" for f in gf.ls("/etc/udev/rules.d")]):
                gf.rm_f(p)
                changed.append(f"removed {p}")
        return "; ".join(changed) or "no MAC-pinned network config found"

    def _remove_tools(self, gf: Guestfish) -> str:
        if self._info.family == "unknown":
            return "unknown distribution: skipped"
        try:
            pkgs = lg.tools_to_remove(gf.command(lg.query_cmd(self._info), rw=False).splitlines(),
                                      keep=self.keep_tool_families)
        except Exception as ex:  # noqa: BLE001
            pkgs, err = [], str(ex)
        else:
            err = ""
        if pkgs:
            try:
                gf.command(lg.remove_cmd(self._info, pkgs))
                return f"removed {pkgs}"
            except Exception as ex:  # noqa: BLE001 - fall back to disabling the services
                err = f"package removal failed ({ex}); "
        disabled = []
        if gf.is_dir("/etc/systemd/system"):
            for w in [f for f in gf.ls("/etc/systemd/system") if f.endswith(".wants")]:
                for u in lg.HYPERVISOR_UNITS:
                    if lg.UNIT_FAMILY.get(u) in self.keep_tool_families:
                        continue
                    p = f"/etc/systemd/system/{w}/{u}"
                    if gf.exists(p):
                        gf.rm_f(p)
                        disabled.append(u)
        if disabled:
            return f"{err}disabled {sorted(set(disabled))}"
        return f"{err}no hypervisor tools found" if not pkgs else f"{err}nothing disabled"

    def _firstboot(self, gf: Guestfish, scripts: list[tuple[str, str]]) -> str:
        if not (gf.exists("/usr/lib/systemd/systemd") or gf.exists("/lib/systemd/systemd")):
            raise RuntimeError("guest has no systemd: first-boot scripts are not supported (run them by hand)")
        files = lg.firstboot_files(scripts)
        for path, (text, mode) in files.items():
            gf.write_file(path, text, mode)
        gf.ln_sf(lg.FIRSTBOOT_UNIT, lg.FIRSTBOOT_WANTS)
        return f"installed {[p.rsplit('/', 1)[1] for p in files if p.startswith(lg.FIRSTBOOT_DIR)]}"

    def _relabel(self, gf: Guestfish) -> str:
        cfg = gf.cat("/etc/selinux/config") if gf.exists("/etc/selinux/config") else None
        if not lg.selinux_enabled(cfg):
            return "SELinux disabled: nothing to do"
        gf.touch("/.autorelabel")
        return "touched /.autorelabel (first boot relabels, then reboots once)"


def _devname_aliases(uuids: dict[str, str]) -> dict[str, str]:
    """The appliance names disks /dev/sdX in VM disk order; the guest's fstab may use sd/vd/xvd/hd.
    Map every alias of each partition to its UUID."""
    import re

    out: dict[str, str] = {}
    for dev, u in uuids.items():
        m = re.match(r"^/dev/sd([a-z]+)(\d*)$", dev)
        if not m:
            continue
        for pfx in ("sd", "vd", "xvd", "hd"):
            out[f"/dev/{pfx}{m.group(1)}{m.group(2)}"] = u
    return out


register(OsFamily.LINUX, "kvm", LinuxKvmStrategy)
