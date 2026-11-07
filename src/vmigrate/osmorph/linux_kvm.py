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
Options (from config osmorph.*): fstab_harden (default True), disable_periodic_fsck (True),
fstab_uuid_map ({"/dev/sdb1": "uuid"}), guestfish (binary path).
"""

from __future__ import annotations

from ..ir import DiskBus, OsFamily
from .base import MorphRequest, MorphStep, MorphStrategy, register
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
    ids = _os_release(gf.cat("/etc/os-release")) if gf.exists("/etc/os-release") else set()
    kvers = sorted(f[len("vmlinuz-"):] for f in gf.ls("/boot") if f.startswith("vmlinuz-") and "rescue" not in f)
    if not kvers:
        raise RuntimeError("no kernels found in /boot")
    debian = bool(ids & _DEBIAN_LIKE)
    done: list[str] = []

    def present(k: str) -> bool:
        builtin = f"/lib/modules/{k}/modules.builtin"
        if gf.exists(builtin) and all(f"/{m}.ko" in gf.cat(builtin) for m in ("virtio_scsi", "virtio_blk", "virtio_pci")):
            return True
        lister = f"lsinitramfs /boot/initrd.img-{k}" if debian else f"lsinitrd --kver {k}"
        try:
            out = gf.command(lister, rw=False)
        except Exception:  # noqa: BLE001 - no initramfs yet / tool missing -> rebuild
            return False
        return all(m in out for m in ("virtio_scsi", "virtio_blk"))

    todo = [k for k in kvers if not present(k)]
    if not todo:
        return f"virtio already available for kernels {kvers}"
    if debian:
        mods = gf.cat("/etc/initramfs-tools/modules") if gf.exists("/etc/initramfs-tools/modules") else ""
        add = [m for m in VIRTIO_DRIVERS if m not in mods.split()]
        if add:
            gf.upload_text(mods.rstrip("\n") + "\n" + "\n".join(add) + "\n", "/etc/initramfs-tools/modules")
        for k in todo:
            gf.command(f"update-initramfs -u -k {k}")
    else:
        for k in todo:
            gf.command(f"dracut -f --kver {k} --add-drivers '{' '.join(VIRTIO_DRIVERS)}'")
    still = [k for k in todo if not present(k)]
    if still:
        raise RuntimeError(f"virtio drivers still missing from the initramfs of {still} after rebuild")
    done = todo
    return f"virtio added to initramfs of {done} ({'update-initramfs' if debian else 'dracut'})"


class LinuxKvmStrategy(MorphStrategy):
    name = "linux-kvm"

    def __init__(self, guestfish_factory=Guestfish):
        self._gf_factory = guestfish_factory

    def plan(self, req: MorphRequest) -> list[MorphStep]:
        steps: list[MorphStep] = []
        opts = req.options
        src_bus = req.spec.boot_disk().bus
        gf = self._gf_factory(req.images, binary=opts.get("guestfish", "guestfish"))
        can_edit = gf.available()

        boot = req.spec.boot_disk()
        if needs_virtio(boot.controller, src_bus, req.target_boot_bus):
            why = (f"boot bus changes {src_bus.value} -> {req.target_boot_bus.value}" if src_bus != req.target_boot_bus
                   else f"guest booted from a {boot.controller or 'unknown'} controller, the target presents virtio")
            steps.append(MorphStep(
                "inject-virtio-initramfs",
                f"{why}: virtio_blk/virtio_scsi/virtio_pci/virtio_net must be in every kernel's initramfs",
                required=True,
                run=(lambda: inject_virtio(gf)) if can_edit else None,
            ))
        if any(n.model and not n.model.startswith("virtio") for n in req.spec.nics):
            models = sorted({n.model for n in req.spec.nics if n.model})
            steps.append(MorphStep(
                "network-naming",
                f"NIC model changes ({', '.join(models)} -> virtio-net): interface names and MACs change; make sure "
                "the guest uses DHCP/cloud-init or update netplan/ifcfg/NetworkManager connections",
                required=False, run=None))

        if opts.get("fstab_harden", True):
            def _fstab() -> str:
                old = gf.cat("/etc/fstab")
                new, changes = harden_fstab(old, device_uuids=opts.get("fstab_uuid_map") or {})
                if not changes:
                    return "fstab already hardened"
                gf.upload_text(new, "/etc/fstab")
                return "; ".join(changes)

            steps.append(MorphStep(
                "fstab-nofail",
                "data mounts: add nofail, set fs_passno=0 (avoid hours-long boot fsck / emergency mode)",
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
        return steps


register(OsFamily.LINUX, "kvm", LinuxKvmStrategy)
