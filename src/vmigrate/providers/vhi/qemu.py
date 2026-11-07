"""Purpose: qemu-img command construction (pure), qcow2 header parsing (pure) and a mockable runner,
for writing VHI VMDS volume backings.

Usage:
    argv = convert_argv("/staging/x.raw", "/mnt/vstorage/.../volume-ID")
    QemuImg().run(argv)
    qcow2_virtual_size(path)                  # reads the 104-byte header, no qemu needed

Pitfall 4: qcow2 writes on vStorage are slow untuned -> -W (out-of-order writes), -m 16
(coroutines), compat=1.1, cluster_size=1M. Pitfall 3: qemu-img is only ever run against LOCAL
staged files and the vStorage backing — never against a Nutanix NFS vdisk.
"""

from __future__ import annotations

import os
import struct
import subprocess
import tempfile
from pathlib import Path

from ...core.errors import ProviderError
from ...core.interfaces import DiskReader

QCOW2_MAGIC = b"QFI\xfb"
DEFAULT_CONVERT_OPTS = ["-p", "-W", "-m", "16"]
DEFAULT_QCOW2_O = "compat=1.1,cluster_size=1048576"


def convert_argv(src_raw: str | Path, dst_qcow2: str | Path, *, qemu_img: str = "qemu-img",
                 extra: list[str] | None = None, o: str = DEFAULT_QCOW2_O) -> list[str]:
    return [qemu_img, "convert", *(extra if extra is not None else DEFAULT_CONVERT_OPTS),
            "-f", "raw", "-O", "qcow2", "-o", o, str(src_raw), str(dst_qcow2)]


def to_raw_argv(src_qcow2: str | Path, dst_raw: str | Path, *, qemu_img: str = "qemu-img") -> list[str]:
    """Replica sync: rebuild the local raw staging image from the committed VMDS backing (a LOCAL
    vStorage read, never Nutanix NFS — pitfall 3 is about hole-probing on NFS vdisks)."""
    return [qemu_img, "convert", "-p", "-m", "16", "-f", "qcow2", "-O", "raw", str(src_qcow2), str(dst_raw)]


def compare_argv(raw: str | Path, qcow2: str | Path, *, qemu_img: str = "qemu-img") -> list[str]:
    return [qemu_img, "compare", "-f", "raw", "-F", "qcow2", "-p", str(raw), str(qcow2)]


def parse_qcow2_header(buf: bytes) -> dict[str, int]:
    if len(buf) < 32 or buf[:4] != QCOW2_MAGIC:
        raise ProviderError("not a qcow2 image (bad magic)")
    version, = struct.unpack(">I", buf[4:8])
    cluster_bits, = struct.unpack(">I", buf[20:24])
    size, = struct.unpack(">Q", buf[24:32])
    return {"version": version, "cluster_bits": cluster_bits, "virtual_size": size}


def qcow2_virtual_size(path: str | Path) -> int:
    with open(path, "rb") as f:
        return parse_qcow2_header(f.read(104))["virtual_size"]


class QemuImg:
    def __init__(self, binary: str = "qemu-img"):
        self.binary = binary

    def run(self, argv: list[str], timeout: float | None = None) -> str:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            raise ProviderError(f"{' '.join(argv[:2])} failed ({p.returncode}): {p.stderr.strip()[:2000]}")
        return p.stdout

    def read_range(self, image: Path, fmt: str, offset: int, length: int) -> bytes:
        """Read [offset, offset+length) from an image via `qemu-img dd` (4 KiB aligned)."""
        if offset % 4096 or length % 4096:
            raise ValueError("qemu-img dd reads must be 4 KiB aligned")
        with tempfile.TemporaryDirectory(prefix="vmigrate-dd-") as td:
            out = Path(td) / "chunk.raw"
            self.run([self.binary, "dd", "-f", fmt, "-O", "raw", "bs=4096",
                      f"skip={offset // 4096}", f"count={length // 4096}", f"if={image}", f"of={out}"])
            data = out.read_bytes()
        return data[:length] + bytes(max(0, length - len(data)))


class QemuImageReader(DiskReader):
    """Sample read-back of a qcow2 for verification (slow per call; only for sampling)."""

    def __init__(self, image: Path, size: int, qemu: QemuImg, fmt: str = "qcow2"):
        self.image, self._size, self.qemu, self.fmt = image, size, qemu, fmt

    @property
    def size(self) -> int:
        return self._size

    def read(self, offset: int, length: int) -> bytes:
        length = max(0, min(length, self._size - offset))
        a_off = offset - offset % 4096
        a_len = -(-(offset + length - a_off) // 4096) * 4096
        data = self.qemu.read_range(self.image, self.fmt, a_off, a_len)
        return data[offset - a_off: offset - a_off + length]


def chown_vstorage(path: Path, group: str = "vstorage-users", mode: int = 0o660) -> None:
    """chown root:<group> + chmod 660 (pitfall 5) — must run as root on the VHI node."""
    import grp

    gid = grp.getgrnam(group).gr_gid
    os.chown(path, 0, gid)
    os.chmod(path, mode)
