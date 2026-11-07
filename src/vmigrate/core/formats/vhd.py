"""Purpose: read-only, pure-Python legacy VHD reader (fixed + dynamic), plus format detection.
Hyper-V Gen1 VMs can still use .vhd; Azure page blobs are fixed VHDs.

Usage:
    r = open_virtual_disk("/mnt/csv1/vm/disk.vhd")     # VhdReader / VhdxReader by magic
    r.read(off, n); r.allocated_ranges()

VHD facts used: 512-byte big-endian footer at the end ("conectix"; current size @48; disk type
@60: 2 fixed, 3 dynamic, 4 differencing; checksum @64 = one's complement of the byte sum). Dynamic:
header at footer.data_offset ("cxsparse"; BAT offset @16; max entries @28; block size @32); BAT
entries are big-endian sector numbers (0xFFFFFFFF = unallocated); each block starts with a sector
bitmap padded to 512 bytes. Differencing VHDs are refused.
"""

from __future__ import annotations

import os
import struct

from ..interfaces import DiskReader
from ..ranges import ByteRange, normalize
from .vhdx import VhdxReader

UNALLOCATED = 0xFFFFFFFF


class VhdError(ValueError):
    pass


def vhd_checksum(footer: bytes) -> int:
    return (~sum(footer[:64] + b"\0\0\0\0" + footer[68:])) & 0xFFFFFFFF


class VhdReader(DiskReader):
    def __init__(self, path: str | os.PathLike[str]):
        self.path = str(path)
        self._fd = os.open(self.path, os.O_RDONLY)
        try:
            self._parse()
        except Exception:
            os.close(self._fd)
            raise

    def _pread(self, off: int, n: int) -> bytes:
        b = os.pread(self._fd, n, off)
        if len(b) != n:
            raise VhdError(f"{self.path}: short read at {off}")
        return b

    def _parse(self) -> None:
        fsize = os.fstat(self._fd).st_size
        if fsize < 512:
            raise VhdError(f"{self.path}: too small for a VHD")
        f = self._pread(fsize - 512, 512)
        if f[:8] != b"conectix":
            raise VhdError(f"{self.path}: not a VHD (no conectix footer)")
        if struct.unpack_from(">I", f, 64)[0] != vhd_checksum(f):
            raise VhdError(f"{self.path}: VHD footer checksum mismatch")
        (self._size,) = struct.unpack_from(">Q", f, 48)
        (self.disk_type,) = struct.unpack_from(">I", f, 60)
        if self.disk_type == 4:
            raise VhdError(f"{self.path}: differencing VHD (has a parent); read the chain-free parent instead")
        if self.disk_type == 2:
            if fsize - 512 < self._size:
                raise VhdError(f"{self.path}: fixed VHD shorter than its virtual size")
            self.block_size = 0
            return
        if self.disk_type != 3:
            raise VhdError(f"{self.path}: unsupported VHD disk type {self.disk_type}")
        (data_off,) = struct.unpack_from(">Q", f, 16)
        h = self._pread(data_off, 1024)
        if h[:8] != b"cxsparse":
            raise VhdError(f"{self.path}: bad dynamic header")
        table_off, = struct.unpack_from(">Q", h, 16)
        self.max_entries, self.block_size = struct.unpack_from(">II", h, 28)
        bitmap = -(-self.block_size // (512 * 8))
        self.bitmap_bytes = -(-bitmap // 512) * 512
        if self.max_entries * self.block_size < self._size:
            raise VhdError(f"{self.path}: BAT smaller than the virtual size")
        self._bat = struct.unpack(f">{self.max_entries}I", self._pread(table_off, 4 * self.max_entries))

    @property
    def size(self) -> int:
        return self._size

    def read(self, offset: int, length: int) -> bytes:
        length = max(0, min(length, self._size - offset))
        if self.disk_type == 2:
            return self._pread(offset, length)
        out = bytearray()
        pos, end = offset, offset + length
        while pos < end:
            b, inb = divmod(pos, self.block_size)
            n = min(self.block_size - inb, end - pos)
            sec = self._bat[b]
            out += bytes(n) if sec == UNALLOCATED else self._pread(sec * 512 + self.bitmap_bytes + inb, n)
            pos += n
        return bytes(out)

    def allocated_ranges(self) -> list[ByteRange] | None:
        if self.disk_type == 2:
            return None
        out = [ByteRange(b * self.block_size, min(self.block_size, self._size - b * self.block_size))
               for b, s in enumerate(self._bat) if s != UNALLOCATED and b * self.block_size < self._size]
        return normalize(out)

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


def open_virtual_disk(path: str | os.PathLike[str]) -> DiskReader:
    """VHDX or VHD by content (not by extension)."""
    with open(path, "rb") as f:
        head = f.read(8)
    if head == b"vhdxfile":
        return VhdxReader(path)
    return VhdReader(path)
