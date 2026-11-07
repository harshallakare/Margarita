"""Purpose: pure /etc/fstab rewriting for migrated Linux guests (pitfalls 6 + 9).

Usage:
    new_text, changes = harden_fstab(old_text, device_uuids={"/dev/sdb1": "1234-..."})

Rules:
  * essential mounts (/, /boot, /boot/efi, /usr by default) are left as-is except UUID rewrite
  * every other non-swap mount gets `nofail` and fs_passno 0 (no hours-long boot-time fsck)
  * /dev/sdX / /dev/vdX / /dev/xvdX device paths with a known UUID become UUID=<uuid>
  * comments, blank lines and unrecognized lines are preserved byte-for-byte
"""

from __future__ import annotations

import re

DEFAULT_ESSENTIAL = frozenset({"/", "/boot", "/boot/efi", "/usr"})
_DEVPATH = re.compile(r"^/dev/(sd|vd|xvd|hd)[a-z]+\d*$")


def harden_fstab(
    text: str,
    *,
    device_uuids: dict[str, str] | None = None,
    essential: frozenset[str] = DEFAULT_ESSENTIAL,
) -> tuple[str, list[str]]:
    device_uuids = device_uuids or {}
    out: list[str] = []
    changes: list[str] = []
    for raw in text.splitlines(keepends=True):
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(raw)
            continue
        f = stripped.split()
        if len(f) < 4:
            out.append(raw)
            continue
        while len(f) < 6:
            f.append("0")
        spec, mnt, fstype, opts, dump, passno = f[:6]
        orig = list(f[:6])
        if _DEVPATH.match(spec) and spec in device_uuids:
            spec = f"UUID={device_uuids[spec]}"
        if fstype != "swap" and mnt not in essential and mnt.startswith("/"):
            optl = opts.split(",")
            if "nofail" not in optl:
                optl.append("nofail")
            opts = ",".join(optl)
            passno = "0"
        new = [spec, mnt, fstype, opts, dump, passno] + f[6:]
        if new[:6] != orig:
            changes.append(f"{mnt}: {' '.join(orig)}  ->  {' '.join(new[:6])}")
            out.append("\t".join(new) + ("\n" if raw.endswith("\n") else ""))
        else:
            out.append(raw)
    return "".join(out), changes


def data_mounts(text: str, essential: frozenset[str] = DEFAULT_ESSENTIAL) -> list[tuple[str, str, str]]:
    """(spec, mountpoint, fstype) of non-essential, non-swap mounts."""
    res = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        f = s.split()
        if len(f) >= 3 and f[2] != "swap" and f[1] not in essential and f[1].startswith("/"):
            res.append((f[0], f[1], f[2]))
    return res
