"""Purpose: the background allocation map: tracker answers (unknown / empty / parts), the copier reading only
written parts of covered chunks and everything else in full, and byte-exact results either way.

Usage: pytest tests/test_allocation_map.py
"""

from __future__ import annotations

import os

from vmigrate.core.allocation import AllocationTracker
from vmigrate.core.fileio import RawFileWriter
from vmigrate.core.interfaces import DiskReader
from vmigrate.core.ranges import ByteRange
from vmigrate.engine.copier import BlockHashes, copy_disk

MIB = 1 << 20
SIZE = 16 * MIB


class Sparse(DiskReader):
    """Data only in MiB 2, 7 and 12; counts what is read."""

    def __init__(self):
        self.data = bytearray(SIZE)
        for m in (2, 7, 12):
            self.data[m * MIB:(m + 1) * MIB] = os.urandom(MIB)
        self.read_bytes = 0

    @property
    def size(self):
        return SIZE

    def read(self, off, n):
        self.read_bytes += n
        return bytes(self.data[off:off + n])


def test_tracker_answers():
    t = AllocationTracker(SIZE)
    assert t.parts(0, MIB) is None                                     # nothing known yet
    t.add([(2 * MIB + 10, 100), (7 * MIB, MIB)], 8 * MIB)
    assert t.parts(0, 2 * MIB) == []                                   # covered, nothing written
    assert t.parts(0, 8 * MIB) == [ByteRange(2 * MIB, MIB), ByteRange(7 * MIB, MIB)]   # aligned to 1 MiB
    assert t.parts(4 * MIB, 8 * MIB) is None                           # runs past what is covered
    assert t.written_bytes == 2 * MIB
    t.finish()
    assert t.parts(8 * MIB, 8 * MIB) == []


def _copy(reader, tracker, tmp_path, chunk=4 * MIB):
    w = RawFileWriter(tmp_path / "t.raw", SIZE)
    h = BlockHashes(SIZE)
    h.fill_zero()
    st = copy_disk(reader, w, chunk_size=chunk, hashes=h, alloc=tracker, prefetch=2)
    w.close()
    return (tmp_path / "t.raw").read_bytes(), st, h


def test_copy_with_complete_map_reads_only_written_parts(tmp_path):
    r = Sparse()
    t = AllocationTracker(SIZE)
    t.add([(2 * MIB, MIB), (7 * MIB, MIB), (12 * MIB, MIB)], SIZE)
    out, st, h = _copy(r, t, tmp_path)
    assert out == bytes(r.data)
    assert r.read_bytes == 3 * MIB and st.bytes_written == 3 * MIB
    assert st.bytes_zero_skipped == SIZE - 3 * MIB
    ref = BlockHashes(SIZE)
    import hashlib
    for i in range(16):
        ref.set(i, hashlib.sha256(bytes(r.data[i * MIB:(i + 1) * MIB])).digest())
    assert h.to_bytes() == ref.to_bytes()                              # checksums match the replica exactly


def test_copy_with_lagging_map_reads_the_rest_in_full(tmp_path):
    r = Sparse()
    t = AllocationTracker(SIZE)
    t.add([(2 * MIB, MIB)], 4 * MIB)                                   # map only reached 4 MiB
    out, st, _ = _copy(r, t, tmp_path)
    assert out == bytes(r.data)
    assert r.read_bytes == MIB + (SIZE - 4 * MIB)                      # first chunk sparse, the rest in full


def test_tracker_ignored_when_target_not_zeroed(tmp_path):
    r = Sparse()
    t = AllocationTracker(SIZE)
    t.finish()                                                         # "nothing written" - must not be trusted
    w = RawFileWriter(tmp_path / "t.raw", SIZE)
    w.prezeroed = False
    copy_disk(r, w, chunk_size=4 * MIB, alloc=t)
    w.close()
    assert (tmp_path / "t.raw").read_bytes() == bytes(r.data) and r.read_bytes == SIZE
