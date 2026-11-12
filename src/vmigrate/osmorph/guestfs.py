"""Purpose: thin, mockable wrapper over the libguestfs `guestfish` CLI for OFFLINE guest edits on
staged disk images (never on a running VM, never on the source).

Usage:
    gf = Guestfish([(Path("disk0.qcow2"), "qcow2"), (Path("disk1.qcow2"), "qcow2")])
    if gf.available(): text = gf.cat("/etc/fstab"); gf.upload_text(new, "/etc/fstab")

All images of the VM are added in disk order so the appliance sees the same multi-disk layout
(LVM spanning disks, /etc/fstab referring to data disks) as the guest.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from ..core import cancel


def octal_mode(mode: int) -> str:
    """File mode as guestfish parses it: C-style octal with a leading 0 ("0755"). Python's
    f"{mode:#o}" gives "0o755", which guestfish rejects ("invalid integer parameter")."""
    return f"0{mode & 0o7777:o}"


class GuestfishError(RuntimeError):
    pass


def _q(path: str) -> str:
    """Quote one guestfish argument (paths with spaces, e.g. NetworkManager connection names)."""
    if "\n" in path:
        raise GuestfishError("newline in guestfish argument")
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Guestfish:
    def __init__(self, images: list[tuple[Path, str]], binary: str = "guestfish"):
        self.images = images
        self.binary = binary

    def available(self) -> bool:
        return bool(self.images) and shutil.which(self.binary) is not None

    def _argv(self, rw: bool, inspect: bool) -> list[str]:
        argv = [self.binary, "--rw" if rw else "--ro"]
        for img, fmt in self.images:
            argv += [f"--format={fmt}", "-a", str(img)]
        if inspect:
            argv.append("-i")
        return argv

    def _run(self, script: str, *, rw: bool, inspect: bool, timeout: int = 1800) -> str:
        p = cancel.run(self._argv(rw, inspect), input=script, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            raise GuestfishError(f"guestfish failed ({p.returncode}): {p.stderr.strip()[:2000]}")
        return p.stdout

    def cat(self, path: str) -> str:
        return self._run(f"cat {_q(path)}\n", rw=False, inspect=True)

    def upload_text(self, text: str, path: str) -> None:
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".vmigrate") as f:
            f.write(text)
            tmp = f.name
        try:
            self._run(f"upload {_q(tmp)} {_q(path)}\n", rw=True, inspect=True)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def list_filesystems(self) -> dict[str, str]:
        out = self._run("run\nlist-filesystems\n", rw=False, inspect=False)
        res = {}
        for line in out.splitlines():
            if ":" in line:
                dev, fs = line.split(":", 1)
                res[dev.strip()] = fs.strip()
        return res

    def ls(self, path: str) -> list[str]:
        return [x for x in self._run(f"ls {_q(path)}\n", rw=False, inspect=True).splitlines() if x.strip()]

    def exists(self, path: str) -> bool:
        return self._run(f"exists {_q(path)}\n", rw=False, inspect=True).strip() == "true"

    def is_dir(self, path: str) -> bool:
        return self._run(f"is-dir {_q(path)}\n", rw=False, inspect=True).strip() == "true"

    def write_file(self, path: str, text: str, mode: int = 0o644) -> None:
        """Upload `text` to `path` (creating parent dirs) and chmod it — one appliance launch."""
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".vmigrate") as f:
            f.write(text)
            tmp = f.name
        parent = path.rsplit("/", 1)[0] or "/"
        try:
            self._run(f"mkdir-p {_q(parent)}\nupload {_q(tmp)} {_q(path)}\nchmod {octal_mode(mode)} {_q(path)}\n",
                      rw=True, inspect=True)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def rm_f(self, path: str) -> None:
        self._run(f"rm-f {_q(path)}\n", rw=True, inspect=True)

    def touch(self, path: str) -> None:
        self._run(f"touch {_q(path)}\n", rw=True, inspect=True)

    def ln_sf(self, target: str, link: str) -> None:
        parent = link.rsplit("/", 1)[0] or "/"
        self._run(f"mkdir-p {_q(parent)}\nln-sf {_q(target)} {_q(link)}\n", rw=True, inspect=True)

    def fs_uuids(self) -> dict[str, str]:
        """{appliance device -> filesystem UUID} for every filesystem with a UUID."""
        res = {}
        for dev in self.list_filesystems():
            try:
                u = self._run(f"run\nvfs-uuid {_q(dev)}\n", rw=False, inspect=False).strip()
            except GuestfishError:
                continue
            if u:
                res[dev] = u
        return res

    def run_script(self, body: str, *, timeout: int = 3600) -> str:
        """Upload a shell script into the guest and run it there (offline, chroot-like)."""
        path = "/tmp/vmigrate-offline.sh"
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".vmigrate") as f:
            f.write(body)
            tmp = f.name
        try:
            return self._run(f"upload {_q(tmp)} {path}\nchmod 0755 {path}\nsh {_q(path)}\nrm-f {path}\n",
                             rw=True, inspect=True, timeout=timeout)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def command(self, cmdline: str, *, rw: bool = True) -> str:
        """Run a command INSIDE the guest filesystem (guestfish `command`, like virt-customize
        --run-command). Used for initramfs rebuilds on the committed target image."""
        if '"' in cmdline:
            raise GuestfishError("guest commands must not contain double quotes")
        return self._run(f'command "{cmdline}"\n', rw=rw, inspect=True, timeout=3600)

    def tune2fs_disable_periodic_fsck(self, devices: list[str]) -> None:
        if not devices:
            return
        script = "run\n" + "".join(f"tune2fs {d} maxmountcount:0 intervalbetweenchecks:0\n" for d in devices)
        self._run(script, rw=True, inspect=False)
