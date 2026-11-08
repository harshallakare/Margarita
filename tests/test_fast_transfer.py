"""Purpose: transfer speed features — pipelined copy (prefetching reader thread), block checksums so a
replica compare never reads the replica back, the live (windowed) rate, and pipelined NBD writes.

Usage: pytest tests/test_fast_transfer.py
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from vmigrate.core.fileio import RawFileReader, RawFileWriter
from vmigrate.core.interfaces import DiskReader
from vmigrate.core.ranges import ByteRange
from vmigrate.engine.copier import HASH_BLOCK, BlockHashes, CopyCancelled, RateMeter, align_ranges, copy_disk
from vmigrate.providers.fake.provider import PatternDisk
from vmigrate.providers.vhi.nbd import NbdClient, NbdDiskWriter, NbdError, agent_command

from .test_replica import rep  # noqa: F401  (fixture)
from .test_platform import plat  # noqa: F401  (fixture)

MB = 1 << 20


class Mutable(DiskReader):
    def __init__(self, data: bytearray, delay: float = 0.0, fail_at: int | None = None):
        self.data, self.delay, self.fail_at = data, delay, fail_at
        self.threads: set[str] = set()

    @property
    def size(self) -> int:
        return len(self.data)

    def read(self, offset: int, length: int) -> bytes:
        self.threads.add(threading.current_thread().name)
        if self.fail_at is not None and offset >= self.fail_at:
            raise OSError("injected read error")
        time.sleep(self.delay)
        return bytes(self.data[offset:offset + length])


def test_prefetch_reads_in_a_background_thread_and_keeps_order(tmp_path):
    src = Mutable(bytearray(PatternDisk("p", 16 * MB).read(0, 16 * MB)))
    with RawFileWriter(tmp_path / "t.raw", 16 * MB) as w:
        seen: list[int] = []
        copy_disk(src, w, chunk_size=MB, prefetch=3, on_progress=lambda off, size: seen.append(off))
    assert (tmp_path / "t.raw").read_bytes() == bytes(src.data)
    assert seen == sorted(seen) and seen[-1] == 16 * MB                  # in order
    assert src.threads == {"disk-prefetch"}                              # reads happened off the main thread


def test_prefetch_overlaps_reading_and_writing(tmp_path):
    class SlowWriter(RawFileWriter):
        def write(self, offset, buf):
            time.sleep(0.05)
            super().write(offset, buf)
    src = Mutable(bytearray(b"\x01" * (8 * MB)), delay=0.05)
    t0 = time.monotonic()
    with SlowWriter(tmp_path / "a.raw", 8 * MB) as w:
        copy_disk(src, w, chunk_size=MB, prefetch=2)
    piped = time.monotonic() - t0
    t0 = time.monotonic()
    with SlowWriter(tmp_path / "b.raw", 8 * MB) as w:
        copy_disk(src, w, chunk_size=MB, prefetch=0)
    serial = time.monotonic() - t0
    assert piped < serial * 0.75                                          # ~0.45 s vs ~0.8 s


def test_prefetch_errors_and_cancel_propagate(tmp_path):
    with RawFileWriter(tmp_path / "t.raw", 8 * MB) as w, pytest.raises(OSError, match="injected"):
        copy_disk(Mutable(bytearray(8 * MB), fail_at=4 * MB), w, chunk_size=MB)
    ev = threading.Event()
    ev.set()
    with RawFileWriter(tmp_path / "u.raw", 8 * MB) as w, pytest.raises(CopyCancelled):
        copy_disk(Mutable(bytearray(8 * MB)), w, chunk_size=MB, cancel=ev)


def test_checksum_compare_never_reads_the_replica_and_writes_only_changes(tmp_path):
    size = 8 * MB
    data = bytearray(PatternDisk("h", size).read(0, size))
    h = BlockHashes(size)
    with RawFileWriter(tmp_path / "r.raw", size) as w:                   # first (full) sync records checksums
        h.fill_zero()
        copy_disk(Mutable(data), w, chunk_size=4 * MB, hashes=h)
    assert h.complete()
    data[3 * MB + 10:3 * MB + 20] = b"X" * 10                              # one block changes on the source
    h2 = BlockHashes(size, h.to_bytes())
    with RawFileWriter(tmp_path / "r.raw", size) as w:
        w.prezeroed = False
        st = copy_disk(Mutable(data), w, chunk_size=4 * MB, hashes=h2)
    assert st.bytes_written == HASH_BLOCK and st.bytes_unchanged == size - HASH_BLOCK
    assert (tmp_path / "r.raw").read_bytes() == bytes(data)
    # a second pass over the same data writes nothing (checksums alone decide; no replica reader exists)
    with RawFileWriter(tmp_path / "r.raw", size) as w:
        w.prezeroed = False
        st = copy_disk(Mutable(data), w, chunk_size=4 * MB, hashes=BlockHashes(size, h2.to_bytes()))
    assert st.bytes_written == 0 and st.bytes_unchanged == size


def test_changed_ranges_grow_to_checksum_blocks():
    r = align_ranges([ByteRange(10, 5), ByteRange(MB + 1, 2), ByteRange(5 * MB - 1, 2)], MB, 6 * MB)
    assert [(x.offset, x.length) for x in r] == [(0, 2 * MB), (4 * MB, 2 * MB)]


def test_rate_meter_shows_current_speed_not_the_average():
    t = [0.0]
    m = RateMeter(window=10, clock=lambda: t[0])
    m.update(0)
    for i in range(1, 21):                       # 20 s at 10 MB/s ...
        t[0] = i
        m.update(i * 10 * MB)
    for i in range(21, 31):                      # ... then 10 s at 100 MB/s
        t[0] = i
        now = m.update(200 * MB + (i - 20) * 100 * MB)
    assert 90 < now <= 101 and m.average(1200 * MB) < 45   # live ~100, average ~40


def test_pipelined_nbd_writes_are_correct_and_errors_surface(tmp_path):
    dev = tmp_path / "dev.img"
    dev.write_bytes(bytes(32 * MB))

    def client(mode: str) -> NbdClient:
        p = subprocess.Popen(agent_command(str(dev), writable=mode == "rw", python=sys.executable, sudo=False),
                             shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        return NbdClient.over_process(p)
    payload = PatternDisk("n", 32 * MB).read(0, 32 * MB)
    with NbdDiskWriter(client("rw"), 32 * MB, prezeroed=False) as w:
        for off in range(0, 32 * MB, 4 * MB):     # many requests in flight
            w.write(off, payload[off:off + 4 * MB])
        w.write_zeroes(MB, MB)
    want = bytearray(payload)
    want[MB:2 * MB] = bytes(MB)
    assert dev.read_bytes() == bytes(want)
    ro = NbdDiskWriter(client("ro"), 32 * MB, prezeroed=False)
    ro.write(0, b"x" * MB)                        # pipelined: the refusal arrives later ...
    with pytest.raises(NbdError, match="errno 1"):
        ro.flush()                                # ... and is raised, never lost


def test_replica_fallback_compare_uses_checksums(rep):  # noqa: F811
    assert rep.run("execute")["status"] == "completed"                # full sync records checksums
    rep.set_source(generation=1, cbt=False)                           # no change tracking available
    x = rep.run("execute")
    assert x["status"] == "completed", x
    last = rep.get()["last_sync"]
    assert last["modes"] == ["compare"]
    reps = [t for t in rep.c.get(f"/executions/{x['id']}/tasks", headers=rep.admin).json()
            if t["kind"] == "replicate_disk"]
    assert reps and all(t["result"]["compare_by"] == "compare (checksums)" for t in reps)   # replica not read
    assert all(t["result"]["checksums"] for t in reps)
    from .test_replica import _expected
    assert all(rep.replica_bytes(i) == _expected(i, 1) for i in range(3))


def test_interrupted_sync_never_leaves_trusted_checksums(rep):  # noqa: F811
    assert rep.run("execute")["status"] == "completed"
    rep.set_source(generation=1, cbt=False, fail_read_at=1 << 20)       # sync dies part-way through writing
    assert rep.run("execute")["status"] == "failed"
    rep.set_source(generation=1, cbt=False)
    x = rep.run("execute")
    assert x["status"] == "completed", x
    reps = [t for t in rep.c.get(f"/executions/{x['id']}/tasks", headers=rep.admin).json()
            if t["kind"] == "replicate_disk"]
    # checksums were cleared when the failed sync started: this one reads the replica, and rebuilds them
    assert reps and all(t["result"]["compare_by"] == "compare (reading replica)" for t in reps)
    assert all(t["result"]["checksums"] for t in reps)
    from .test_replica import _expected
    assert all(rep.replica_bytes(i) == _expected(i, 1) for i in range(3))


def test_partial_checksums_round_trip_and_invalidate():
    h = BlockHashes(4 * MB + 5)                      # 5 blocks, the last one short
    assert not h.has_base and h.coverage() == 0
    h.fill_zero()
    full = h.to_bytes()
    assert len(full) == 5 * 32 and BlockHashes(4 * MB + 5, full).complete()      # old (complete) format still loads
    h.invalidate([ByteRange(MB + 10, 1), ByteRange(4 * MB, 5)])                   # touches blocks 1 and 4
    part = BlockHashes(4 * MB + 5, h.to_bytes())
    assert part.has_base and not part.complete() and part.coverage() == 0.6
    assert part.get(0) is not None and part.get(1) is None and part.get(4) is None


def test_partial_checksums_read_back_only_the_unknown_blocks(tmp_path):
    size = 8 * MB
    data = bytearray(PatternDisk("q", size).read(0, size))
    h = BlockHashes(size)
    with RawFileWriter(tmp_path / "r.raw", size) as w:
        h.fill_zero()
        copy_disk(Mutable(data), w, chunk_size=4 * MB, hashes=h)
    h.invalidate([ByteRange(2 * MB, 2 * MB)])        # blocks 2-3: written by a sync whose checksums were lost
    data[2 * MB + 7] ^= 0xFF                         # one byte in an unknown block changes ...
    data[6 * MB + 7] ^= 0xFF                         # ... and one in a known block

    class Replica(RawFileReader):
        read_bytes = 0

        def read(self, offset, length):
            Replica.read_bytes += length
            return super().read(offset, length)
    rep = Replica(tmp_path / "r.raw")
    h2 = BlockHashes(size, h.to_bytes())
    with RawFileWriter(tmp_path / "r.raw", size) as w:
        w.prezeroed = False
        st = copy_disk(Mutable(data), w, chunk_size=4 * MB, hashes=h2, compare=rep)
    rep.close()
    assert Replica.read_bytes == 2 * MB                               # only the unknown blocks were read back
    assert st.bytes_written == (64 << 10) + MB      # unknown block: the 64 KiB piece; known block: the whole 1 MiB
    assert (tmp_path / "r.raw").read_bytes() == bytes(data) and h2.complete()


def test_cbt_sync_keeps_checksums_and_one_failed_sync_loses_only_its_blocks(rep):  # noqa: F811
    lim = 2 * MB                                  # writes land in the first 2 of 9 MiB (localized, like real VMs)

    def src(gen, **kw):
        rep.set_source(generation=gen, change_limit=lim, **kw)

    def expected(i, gen):
        return PatternDisk(f"RIS-Sainath:{i}", 9 * MB, generation=gen, change_limit=lim).read(0, 9 * MB)

    def disk_results(x):
        return [t["result"] for t in rep.c.get(f"/executions/{x['id']}/tasks", headers=rep.admin).json()
                if t["kind"] == "replicate_disk"]
    src(0)
    assert rep.run("execute")["status"] == "completed"                # full: complete checksums
    src(1)                                                            # cbt touches <= 2 blocks per disk
    x = rep.run("execute")
    res = disk_results(x)
    assert all(r["mode"] == "cbt" and r["checksum_coverage"] >= 7 / 9 for r in res), res   # was: all dropped
    src(2, cbt=False, fail_read_at=1 << 20)                           # a compare sync dies: checksums cleared
    assert rep.run("execute")["status"] == "failed"
    src(2)
    assert rep.run("execute")["status"] == "completed"                # cbt: nothing to build on yet
    src(3, cbt=False)                                                 # compare reads the replica, rebuilds all
    x = rep.run("execute")
    assert x["status"] == "completed", x
    assert all(r["compare_by"] == "compare (reading replica)" and r["checksums"] for r in disk_results(x))
    src(4)                                                            # cbt: unaligned writes forget only their blocks
    x = rep.run("execute")
    res = disk_results(x)
    assert all(r["mode"] == "cbt" and 7 / 9 <= r["checksum_coverage"] for r in res), res
    src(5, cbt=False)                                                 # fallback reads back only those blocks
    x = rep.run("execute")
    assert x["status"] == "completed", x
    res = disk_results(x)
    assert all(r["checksums"] and r["compare_by"].startswith("compare (checksums") for r in res), res
    assert all(rep.replica_bytes(i) == expected(i, 5) for i in range(3))
