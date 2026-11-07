"""Purpose: read-only, pure-Python VHDX reader (MS-VHDX v1) for fixed and dynamic images. Used
by the Hyper-V source to read the frozen parent disk of a recovery snapshot through an SMB/CSV
mount; reports allocated blocks so the copier skips never-written space.

Usage:
    with VhdxReader("/mnt/csv1/VMs/web/disk0.vhdx") as r:
        r.size; r.read(off, n); r.allocated_ranges()

Validation (fail loud, never guess):
  * file identifier "vhdxfile"; both headers checked with CRC-32C, newest valid one wins
  * a non-zero LogGuid means unreplayed log entries -> refused (image not consistent)
  * region table CRC-32C (primary, then backup); BAT + metadata regions must be present
  * differencing images (HasParent) are refused: the caller must read a chain-free parent
Layout facts used: BAT entries are 64-bit (state = bits 0-2, file offset in MiB = bits 20-63);
after every `chunk_ratio` payload entries comes one sector-bitmap entry; chunk_ratio =
(2^23 * logical_sector_size) / block_size. Payload state 6 = FULLY_PRESENT; 0/1/2/3 read as zeros.
"""

from __future__ import annotations

import os
import struct
import uuid

from ..crc32c import crc32c
from ..interfaces import DiskReader
from ..ranges import ByteRange, normalize

MIB = 1 << 20
KIB64 = 64 << 10
BAT_GUID = uuid.UUID("2DC27766-F623-4200-9D64-115E9BFD4A08")
META_GUID = uuid.UUID("8B7CA206-4790-4B9A-B8FE-575F050F886E")
FILE_PARAMS = uuid.UUID("CAA16737-FA36-4D43-B3B6-33F0AA44E76B")
VDISK_SIZE = uuid.UUID("2FA54224-CD1B-4876-B211-5DBED83BF4B8")
LOGICAL_SECTOR = uuid.UUID("8141BF1D-A96F-4709-BA47-F233A8FAAB5F")
PAYLOAD_FULLY_PRESENT = 6
PAYLOAD_PARTIALLY_PRESENT = 7


class VhdxError(ValueError):
    pass


def _with_zero_checksum(buf: bytes, at: int = 4) -> bytes:
    return buf[:at] + b"\0\0\0\0" + buf[at + 4:]


class VhdxReader(DiskReader):
    def __init__(self, path: str | os.PathLike[str]):
        self.path = str(path)
        self._fd = os.open(self.path, os.O_RDONLY)
        try:
            self._parse()
        except Exception:
            os.close(self._fd)
            raise

    def _pread(self, off: int, n: int) -> bytes:
        buf = os.pread(self._fd, n, off)
        if len(buf) != n:
            raise VhdxError(f"{self.path}: short read at {off} ({len(buf)}/{n})")
        return buf

    # ------------------------------------------------------------------ parsing
    def _parse(self) -> None:
        if self._pread(0, 8) != b"vhdxfile":
            raise VhdxError(f"{self.path}: not a VHDX file")
        best = None
        for off in (KIB64, 2 * KIB64):
            h = self._pread(off, 4096)
            if h[:4] != b"head":
                continue
            (cs,) = struct.unpack_from("<I", h, 4)
            if crc32c(_with_zero_checksum(h)) != cs:
                continue
            seq = struct.unpack_from("<Q", h, 8)[0]
            if best is None or seq > best[0]:
                best = (seq, h)
        if best is None:
            raise VhdxError(f"{self.path}: no valid VHDX header (both copies fail CRC-32C)")
        log_guid = best[1][48:64]
        if log_guid != bytes(16):
            raise VhdxError(f"{self.path}: VHDX log has unreplayed entries (image not cleanly closed); "
                            "refusing to read an inconsistent image")
        regions = None
        for off in (3 * KIB64, 4 * KIB64):
            rt = self._pread(off, KIB64)
            if rt[:4] == b"regi" and crc32c(_with_zero_checksum(rt)) == struct.unpack_from("<I", rt, 4)[0]:
                regions = rt
                break
        if regions is None:
            raise VhdxError(f"{self.path}: region table corrupt (both copies fail CRC-32C)")
        (count,) = struct.unpack_from("<I", regions, 8)
        reg: dict[uuid.UUID, tuple[int, int]] = {}
        for i in range(count):
            e = regions[16 + 32 * i: 48 + 32 * i]
            g = uuid.UUID(bytes_le=e[:16])
            fo, ln = struct.unpack_from("<QI", e, 16)
            reg[g] = (fo, ln)
        if BAT_GUID not in reg or META_GUID not in reg:
            raise VhdxError(f"{self.path}: BAT or metadata region missing")
        moff, mlen = reg[META_GUID]
        meta = self._pread(moff, mlen)
        if meta[:8] != b"metadata":
            raise VhdxError(f"{self.path}: bad metadata header")
        (mcount,) = struct.unpack_from("<H", meta, 10)
        items: dict[uuid.UUID, bytes] = {}
        for i in range(mcount):
            e = meta[32 + 32 * i: 64 + 32 * i]
            g = uuid.UUID(bytes_le=e[:16])
            o, ln = struct.unpack_from("<II", e, 16)
            items[g] = meta[o:o + ln]
        for need in (FILE_PARAMS, VDISK_SIZE, LOGICAL_SECTOR):
            if need not in items:
                raise VhdxError(f"{self.path}: required metadata item {need} missing")
        self.block_size, flags = struct.unpack_from("<II", items[FILE_PARAMS])
        if flags & 2:
            raise VhdxError(f"{self.path}: differencing VHDX (has a parent); read the chain-free parent instead")
        (self._size,) = struct.unpack_from("<Q", items[VDISK_SIZE])
        (self.logical_sector,) = struct.unpack_from("<I", items[LOGICAL_SECTOR])
        if self.block_size < MIB or self.block_size & (self.block_size - 1):
            raise VhdxError(f"{self.path}: invalid block size {self.block_size}")
        self.chunk_ratio = ((1 << 23) * self.logical_sector) // self.block_size
        self.nblocks = -(-self._size // self.block_size)
        n_entries = self.nblocks + (self.nblocks - 1) // self.chunk_ratio
        boff, blen = reg[BAT_GUID]
        if n_entries * 8 > blen:
            raise VhdxError(f"{self.path}: BAT too small for {self.nblocks} blocks")
        raw = self._pread(boff, n_entries * 8)
        self._bat = struct.unpack(f"<{n_entries}Q", raw)

    def _entry(self, block: int) -> tuple[int, int]:
        e = self._bat[block + block // self.chunk_ratio]
        return e & 7, (e >> 20) * MIB

    # ------------------------------------------------------------------ DiskReader
    @property
    def size(self) -> int:
        return self._size

    def read(self, offset: int, length: int) -> bytes:
        length = max(0, min(length, self._size - offset))
        out = bytearray()
        pos = offset
        end = offset + length
        while pos < end:
            b, inb = divmod(pos, self.block_size)
            n = min(self.block_size - inb, end - pos)
            state, fo = self._entry(b)
            if state == PAYLOAD_FULLY_PRESENT:
                out += self._pread(fo + inb, n)
            elif state == PAYLOAD_PARTIALLY_PRESENT:
                raise VhdxError(f"{self.path}: partially-present block {b} in a non-differencing image")
            else:
                out += bytes(n)
            pos += n
        return bytes(out)

    def allocated_ranges(self) -> list[ByteRange]:
        out = []
        for b in range(self.nblocks):
            if self._entry(b)[0] == PAYLOAD_FULLY_PRESENT:
                start = b * self.block_size
                out.append(ByteRange(start, min(self.block_size, self._size - start)))
        return normalize(out)

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1
