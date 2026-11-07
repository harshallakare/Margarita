"""Purpose: VHDX / VHD reader tests against spec-shaped synthetic images: allocated vs unallocated
blocks, reads across block boundaries, header fallback, and every refusal (corrupt, dirty log,
differencing, bad checksum).

Usage: pytest tests/test_vdisk_formats.py
"""

from __future__ import annotations

import pytest

from vmigrate.core.formats.vhd import VhdError, VhdReader, open_virtual_disk
from vmigrate.core.formats.vhdx import VhdxError, VhdxReader
from vmigrate.providers.fake.provider import PatternDisk

from .vdisk_builders import MIB, write_vhd, write_vhdx

SIZE = 5 * MIB + 4096          # deliberately not block aligned


def expected(blocks: dict[int, bytes], bs: int, size: int) -> bytes:
    out = bytearray(size)
    for b, d in blocks.items():
        out[b * bs:b * bs + len(d)] = d[: max(0, size - b * bs)]
    return bytes(out)


def blocks_for(bs: int, which: list[int]) -> dict[int, bytes]:
    return {b: PatternDisk(f"b{b}", bs).read(0, bs) for b in which}


def test_vhdx_dynamic_reads_and_allocation(tmp_path):
    bl = blocks_for(MIB, [0, 2, 5])
    p = tmp_path / "d.vhdx"
    write_vhdx(p, SIZE, bl)
    exp = expected(bl, MIB, SIZE)
    with VhdxReader(p) as r:
        assert r.size == SIZE and r.block_size == MIB
        assert r.read(0, SIZE) == exp
        assert r.read(MIB - 100, 300) == exp[MIB - 100:MIB + 200]          # across a block boundary
        assert [(x.offset, x.length) for x in r.allocated_ranges()] == [(0, MIB), (2 * MIB, MIB), (5 * MIB, 4096)]
    assert isinstance(open_virtual_disk(p), VhdxReader)


def test_vhdx_falls_back_to_valid_header_and_refuses_bad_images(tmp_path):
    p = tmp_path / "a.vhdx"
    write_vhdx(p, 2 * MIB, blocks_for(MIB, [1]), corrupt_header=1)   # newest copy corrupt -> older one used
    with VhdxReader(p) as r:
        assert r.read(MIB, 16) == blocks_for(MIB, [1])[1][:16]
    write_vhdx(p, 2 * MIB, {}, log_guid=b"\x01" * 16)
    with pytest.raises(VhdxError, match="unreplayed"):
        VhdxReader(p)
    write_vhdx(p, 2 * MIB, {}, has_parent=True)
    with pytest.raises(VhdxError, match="differencing"):
        VhdxReader(p)
    raw = bytearray(p.read_bytes())
    raw[3 * 65536 + 40] ^= 1
    raw[4 * 65536 + 40] ^= 1
    p.write_bytes(bytes(raw))
    with pytest.raises(VhdxError):
        VhdxReader(p)


def test_vhd_dynamic_and_fixed(tmp_path):
    bs = 2 * MIB
    bl = blocks_for(bs, [1])
    p = tmp_path / "d.vhd"
    write_vhd(p, SIZE, bl, block_size=bs)
    with open_virtual_disk(p) as r:
        assert isinstance(r, VhdReader) and r.read(0, SIZE) == expected(bl, bs, SIZE)
        assert [(x.offset, x.length) for x in r.allocated_ranges()] == [(bs, bs)]
    data = PatternDisk("fixed", 3 * MIB).read(0, 3 * MIB)
    write_vhd(p, 3 * MIB, fixed_data=data)
    with VhdReader(p) as r:
        assert r.read(12345, 70000) == data[12345:82345] and r.allocated_ranges() is None


def test_vhd_refusals(tmp_path):
    p = tmp_path / "x.vhd"
    write_vhd(p, 2 * MIB, {}, disk_type=4)
    with pytest.raises(VhdError, match="differencing"):
        VhdReader(p)
    write_vhd(p, 2 * MIB, fixed_data=bytes(10))
    raw = bytearray(p.read_bytes())
    raw[-512 + 50] ^= 1
    p.write_bytes(bytes(raw))
    with pytest.raises(VhdError, match="checksum"):
        VhdReader(p)
