"""Purpose: build small, spec-shaped VHDX and VHD images for reader tests (no Windows needed).

Usage:
    write_vhdx(path, size, blocks={0: data, 3: data}, block_size=1 << 20)   # dynamic VHDX
    write_vhd(path, size, blocks={...}, block_size=2 << 20)                 # dynamic VHD
    write_vhd(path, size, fixed_data=b"...")                                # fixed VHD
Unlisted blocks are left unallocated (read back as zeros).
"""

from __future__ import annotations

import struct
import uuid

from vmigrate.core.crc32c import crc32c
from vmigrate.core.formats.vhd import vhd_checksum
from vmigrate.core.formats.vhdx import BAT_GUID, FILE_PARAMS, LOGICAL_SECTOR, META_GUID, VDISK_SIZE

MIB = 1 << 20
K64 = 64 << 10


def _crc_into(buf: bytearray, at: int = 4) -> None:
    buf[at:at + 4] = b"\0\0\0\0"
    buf[at:at + 4] = struct.pack("<I", crc32c(bytes(buf)))


def write_vhdx(path, size: int, blocks: dict[int, bytes], *, block_size: int = MIB, has_parent: bool = False,
               log_guid: bytes = bytes(16), corrupt_header: int | None = None) -> None:
    lss = 512
    chunk_ratio = ((1 << 23) * lss) // block_size
    nblocks = -(-size // block_size)
    n_entries = nblocks + (nblocks - 1) // chunk_ratio
    meta_off, meta_len = MIB, MIB
    bat_off = 2 * MIB
    bat_len = max(MIB, -(-n_entries * 8 // MIB) * MIB)
    data_off = bat_off + bat_len
    f = bytearray(data_off + len(blocks) * block_size)
    f[0:8] = b"vhdxfile"
    for i, off in enumerate((K64, 2 * K64)):
        h = bytearray(4096)
        h[0:4] = b"head"
        struct.pack_into("<Q", h, 8, 10 + i)          # sequence: second copy is newer
        h[48:64] = log_guid
        struct.pack_into("<HHIQ", h, 64, 0, 1, MIB, MIB)
        _crc_into(h)
        if corrupt_header == i:
            h[100] ^= 0xFF
        f[off:off + 4096] = h
    rt = bytearray(K64)
    rt[0:4] = b"regi"
    struct.pack_into("<I", rt, 8, 2)
    for i, (g, o, ln) in enumerate(((BAT_GUID, bat_off, bat_len), (META_GUID, meta_off, meta_len))):
        e = 16 + 32 * i
        rt[e:e + 16] = g.bytes_le
        struct.pack_into("<QII", rt, e + 16, o, ln, 1)
    _crc_into(rt)
    f[3 * K64:4 * K64] = rt
    f[4 * K64:5 * K64] = rt
    meta = bytearray(meta_len)
    meta[0:8] = b"metadata"
    items = [(FILE_PARAMS, struct.pack("<II", block_size, 2 if has_parent else 0)),
             (VDISK_SIZE, struct.pack("<Q", size)), (LOGICAL_SECTOR, struct.pack("<I", lss))]
    struct.pack_into("<H", meta, 10, len(items))
    off = K64
    for i, (g, data) in enumerate(items):
        e = 32 + 32 * i
        meta[e:e + 16] = g.bytes_le
        struct.pack_into("<II", meta, e + 16, off, len(data))
        meta[off:off + len(data)] = data
        off += 4096
    f[meta_off:meta_off + meta_len] = meta
    bat = [0] * n_entries
    for k, (b, data) in enumerate(sorted(blocks.items())):
        fo = data_off + k * block_size
        bat[b + b // chunk_ratio] = 6 | ((fo // MIB) << 20)
        f[fo:fo + len(data)] = data
    f[bat_off:bat_off + 8 * n_entries] = struct.pack(f"<{n_entries}Q", *bat)
    with open(path, "wb") as fh:
        fh.write(f)


def _footer(size: int, disk_type: int, data_offset: int) -> bytes:
    ft = bytearray(512)
    ft[0:8] = b"conectix"
    struct.pack_into(">II", ft, 8, 2, 0x10000)
    struct.pack_into(">Q", ft, 16, data_offset)
    struct.pack_into(">QQ", ft, 40, size, size)
    struct.pack_into(">I", ft, 60, disk_type)
    ft[68:84] = uuid.uuid4().bytes
    struct.pack_into(">I", ft, 64, vhd_checksum(bytes(ft)))
    return bytes(ft)


def write_vhd(path, size: int, blocks: dict[int, bytes] | None = None, *, block_size: int = 2 * MIB,
              fixed_data: bytes | None = None, disk_type: int | None = None) -> None:
    if fixed_data is not None:
        with open(path, "wb") as fh:
            fh.write(fixed_data.ljust(size, b"\0") + _footer(size, disk_type or 2, 0xFFFFFFFFFFFFFFFF))
        return
    blocks = blocks or {}
    max_entries = -(-size // block_size)
    bat_off = 512 + 1024
    bat_len = -(-4 * max_entries // 512) * 512
    data_start = bat_off + bat_len
    bitmap = 512
    out = bytearray(data_start + len(blocks) * (bitmap + block_size))
    out[0:512] = _footer(size, disk_type or 3, 512)
    h = bytearray(1024)
    h[0:8] = b"cxsparse"
    struct.pack_into(">QQ", h, 8, 0xFFFFFFFFFFFFFFFF, bat_off)
    struct.pack_into(">III", h, 24, 0x10000, max_entries, block_size)
    out[512:1536] = h
    bat = [0xFFFFFFFF] * max_entries
    for k, (b, data) in enumerate(sorted(blocks.items())):
        start = data_start + k * (bitmap + block_size)
        bat[b] = start // 512
        out[start:start + bitmap] = b"\xff" * bitmap
        out[start + bitmap:start + bitmap + len(data)] = data
    out[bat_off:bat_off + 4 * max_entries] = struct.pack(f">{max_entries}I", *bat)
    with open(path, "wb") as fh:
        fh.write(out + _footer(size, disk_type or 3, 512))
