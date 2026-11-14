"""Purpose: the data-movement primitive — pipe DiskReader -> DiskWriter in large linear chunks with
zero-skipping, progress callbacks, resume-from-watermark and optional changed-range copy.

Usage:
    stats = copy_disk(reader, writer, chunk_size=64 << 20, start_offset=state.bytes_copied,
                      on_checkpoint=lambda off: state.save_watermark(off))

Pipelined: a reader thread prefetches the next `prefetch` chunks while the current one is written, and the
zero check + per-block SHA-256 of each chunk run on a small thread pool as soon as it is read, so the copy
thread only hands chunks to the writer (seen live: send -> checksum -> copy in sequence held one disk near
125 MB/s with the network, SSH and the helper all partly idle). Order of writes, checkpoints and progress is
unchanged.

Compare mode (replica sync without source change tracking):
  * `hashes` with stored checksums (BlockHashes.has_base) — each 1 MiB block of the source is hashed
    and compared with the checksum recorded when that block was last written; only differing blocks
    are written. The replica is never read back (the Coriolis approach: checksums kept from the
    previous run instead of re-reading the target).
  * `compare=<reader of the replica>` — read the replica back and compare; with partial checksums,
    only the blocks whose checksum is unknown are read back.
`hashes` is also maintained on full / changed-range copies so the next fallback can use it.

Resume contract: on_checkpoint(offset) is called only AFTER writer.flush(), so a persisted
watermark always means "everything below this offset is durable on the target".
"""

from __future__ import annotations

import hashlib
import queue
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from ..core.allocation import AllocationTracker
from ..core.interfaces import DiskReader, DiskWriter
from ..core.ranges import ByteRange, clip, split

DEFAULT_CHUNK = 64 << 20  # dd bs=64M — what worked on the reference path
HASH_BLOCK = 1 << 20      # checksum granularity for replica compare (8 MB of SHA-256 per 250 GiB)
DIGEST = 32


class CopyCancelled(Exception):
    pass


@dataclass
class CopyStats:
    bytes_total: int = 0
    bytes_read: int = 0
    bytes_written: int = 0
    bytes_zero_skipped: int = 0
    bytes_unchanged: int = 0      # compare mode: identical to the replica, not written
    seconds: float = 0.0
    resumed_from: int = 0

    @property
    def mb_per_s(self) -> float:
        return (self.bytes_read / (1 << 20)) / self.seconds if self.seconds > 0 else 0.0


def _is_zero(buf: bytes, _cache: dict[int, bytes] = {}) -> bool:
    n = len(buf)
    z = _cache.get(n)
    if z is None:
        z = _cache.setdefault(n, bytes(n))
    return buf == z  # memcmp speed


def _zero_digest(n: int, _cache: dict[int, bytes] = {}) -> bytes:
    d = _cache.get(n)
    if d is None:
        d = _cache.setdefault(n, hashlib.sha256(bytes(n)).digest())
    return d


class BlockHashes:
    """SHA-256 per HASH_BLOCK of one disk, as written to the replica, with per-block validity.
    `base` = checksums saved by an earlier sync: n*32 bytes (all blocks known) or n*32 + n bytes
    (digests + one known-flag byte per block). A change-tracking sync invalidates only the blocks it
    may touch, so one interrupted or scattered sync no longer throws every checksum away (seen live:
    after one stopped sync, every later cbt sync ran without checksums)."""

    def __init__(self, size: int, base: bytes | None = None, block: int = HASH_BLOCK):
        self.size, self.block = size, block
        self.n = -(-size // block) if size else 0
        self.data = bytearray(self.n * DIGEST)
        self.known = bytearray(self.n)
        if base is not None and len(base) == self.n * DIGEST:
            self.data[:] = base
            self.known = bytearray(b"\x01") * self.n
        elif base is not None and len(base) == self.n * (DIGEST + 1):
            self.data[:] = base[:self.n * DIGEST]
            self.known = bytearray(1 if b else 0 for b in base[self.n * DIGEST:])
        self.has_base = any(self.known)        # some checksums came from an earlier sync

    def get(self, i: int) -> bytes | None:
        return bytes(self.data[i * DIGEST:(i + 1) * DIGEST]) if self.known[i] else None

    def set(self, i: int, digest: bytes) -> None:
        self.data[i * DIGEST:(i + 1) * DIGEST] = digest
        self.known[i] = 1

    def invalidate(self, ranges: Iterable[ByteRange]) -> None:
        """Forget every block a write to `ranges` may touch."""
        for r in ranges:
            if r.length <= 0:
                continue
            a, b = r.offset // self.block, min(self.n, -(-r.end // self.block))
            self.known[a:b] = bytes(max(0, b - a))

    def fill_zero(self) -> None:
        """A fresh, zeroed target: every block not written below reads as zeros."""
        for i in range(self.n):
            self.set(i, _zero_digest(min(self.block, self.size - i * self.block)))

    def complete(self) -> bool:
        return all(self.known)

    def any_known(self) -> bool:
        return any(self.known)

    def coverage(self) -> float:
        return (sum(self.known) / self.n) if self.n else 1.0

    def to_bytes(self) -> bytes:
        return bytes(self.data) if self.complete() else bytes(self.data) + bytes(self.known)


def align_ranges(ranges: Iterable[ByteRange], block: int, size: int) -> list[ByteRange]:
    """Grow changed ranges to whole checksum blocks (so each touched block can be re-hashed)."""
    out: list[ByteRange] = []
    for r in sorted(ranges, key=lambda r: r.offset):
        a, b = (r.offset // block) * block, min(size, -(-r.end // block) * block)
        if out and a <= out[-1].end:
            out[-1] = ByteRange(out[-1].offset, max(out[-1].end, b) - out[-1].offset)
        else:
            out.append(ByteRange(a, b - a))
    return out


class RateMeter:
    """Current throughput over a sliding window (the UI used to show the average since start, which
    looked like a slow ramp after a data-heavy beginning)."""

    def __init__(self, window: float = 10.0, clock: Callable[[], float] = time.monotonic):
        self.window, self.clock = window, clock
        self.samples: list[tuple[float, int]] = []
        self.t0: float | None = None
        self.first: int | None = None

    def update(self, position: int) -> float:
        now = self.clock()
        if self.t0 is None:
            self.t0, self.first = now, position
        self.samples.append((now, position))
        while len(self.samples) > 2 and now - self.samples[0][0] > self.window:
            self.samples.pop(0)
        t, p = self.samples[0]
        return ((position - p) / (1 << 20)) / (now - t) if now - t > 0.5 else self.average(position)

    def average(self, position: int) -> float:
        if self.t0 is None:
            return 0.0
        el = self.clock() - self.t0
        return ((position - (self.first or 0)) / (1 << 20)) / el if el > 0 else 0.0


_END = object()


@dataclass
class Analysis:
    zero: bool
    digests: list[bytes] | None       # SHA-256 per hash block, when checksums are kept


def analyze(buf: bytes, block: int | None) -> Analysis:
    """Zero check + per-block digests of one chunk (hashlib releases the GIL: runs beside the copy)."""
    zero = _is_zero(buf)
    if block is None:
        return Analysis(zero, None)
    view = memoryview(buf)
    n = len(buf)
    if zero:
        return Analysis(True, [_zero_digest(min(block, n - o)) for o in range(0, n, block)])
    return Analysis(False, [hashlib.sha256(view[o:o + block]).digest() for o in range(0, n, block)])


def _read_piece(reader: DiskReader, p: ByteRange,
                alloc: AllocationTracker | None) -> tuple[bytes | None, list[ByteRange] | None]:
    """(data, parts). parts None: the whole piece was read. [] : nothing written there (data None).
    Otherwise only `parts` were read; the rest of `data` is zeros (a fresh target already reads zeros)."""
    parts = alloc.parts(p.offset, p.length) if alloc is not None else None
    if parts is None:
        return reader.read(p.offset, p.length), None
    if not parts:
        return None, []
    if len(parts) == 1 and parts[0].offset == p.offset and parts[0].length == p.length:
        return reader.read(p.offset, p.length), None
    buf = bytearray(p.length)
    for r in parts:
        buf[r.offset - p.offset:r.end - p.offset] = reader.read(r.offset, r.length)
    return bytes(buf), parts


def _prefetch(reader: DiskReader, pieces: list[ByteRange], depth: int, stop: threading.Event,
              alloc: AllocationTracker | None = None) -> Iterator[tuple[ByteRange, bytes | None, list | None]]:
    """Yield (piece, data, parts) in order while a background thread reads ahead `depth` chunks."""
    if depth <= 0:
        for p in pieces:
            yield (p, *_read_piece(reader, p, alloc))
        return
    q: queue.Queue = queue.Queue(maxsize=depth)

    def run() -> None:
        try:
            for p in pieces:
                if stop.is_set():
                    return
                buf, parts = _read_piece(reader, p, alloc)
                while not stop.is_set():
                    try:
                        q.put((p, buf, parts), timeout=0.5)
                        break
                    except queue.Full:
                        continue
            q.put(_END)
        except BaseException as e:  # noqa: BLE001 - re-raised in the consumer
            q.put(e)

    t = threading.Thread(target=run, name="disk-prefetch", daemon=True)
    t.start()
    try:
        while True:
            item = q.get()
            if item is _END:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        stop.set()
        t.join(timeout=30)


def copy_disk(
    reader: DiskReader,
    writer: DiskWriter,
    *,
    chunk_size: int = DEFAULT_CHUNK,
    start_offset: int = 0,
    ranges: Iterable[ByteRange] | None = None,
    checkpoint_every: int = 1 << 30,
    on_progress: Callable[[int, int], None] | None = None,
    on_checkpoint: Callable[[int], None] | None = None,
    cancel: threading.Event | None = None,
    compare: DiskReader | None = None,
    compare_block: int = 64 << 10,
    hashes: BlockHashes | None = None,
    prefetch: int = 2,
    alloc: AllocationTracker | None = None,
) -> CopyStats:
    if writer.size < reader.size:
        raise ValueError(f"target disk ({writer.size} B) smaller than source ({reader.size} B)")
    size = reader.size
    if hashes is not None and (chunk_size % hashes.block or start_offset % hashes.block):
        hashes = None                     # misaligned: never record checksums we cannot trust
    if ranges is None:
        work = [ByteRange(start_offset, size - start_offset)] if start_offset < size else []
    else:
        if hashes is not None:            # changed ranges: re-hash whole blocks they touch
            ranges = align_ranges(ranges, hashes.block, size)
        work = [r for r in clip(ranges, size) if r.end > start_offset]
        work = [ByteRange(max(r.offset, start_offset), r.end - max(r.offset, start_offset)) for r in work]
    pieces = split(work, chunk_size)
    by_hash = hashes is not None and hashes.has_base and ranges is None
    if alloc is not None and not (writer.prezeroed and compare is None and not by_hash):
        alloc = None                      # skipping unwritten space is only right onto a fresh, zeroed target

    stats = CopyStats(bytes_total=size, resumed_from=start_offset)
    t0 = time.monotonic()
    since_ckpt = 0
    high = start_offset
    stop = threading.Event()
    hblock = hashes.block if hashes is not None else None
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="disk-hash")
    ahead: deque[tuple[ByteRange, bytes | None, list | None, Future | Analysis]] = deque()

    def empty(n: int) -> Analysis:
        return Analysis(True, [_zero_digest(min(hblock, n - o)) for o in range(0, n, hblock)] if hblock else None)

    def chunks() -> Iterator[tuple[ByteRange, bytes | None, list | None, Analysis]]:
        """Chunks in order with their analysis; the next chunk's analysis runs while this one is written."""
        src = _prefetch(reader, pieces, prefetch, stop, alloc)
        for p, buf, parts in src:
            ahead.append((p, buf, parts, empty(p.length) if buf is None else pool.submit(analyze, buf, hblock)))
            if len(ahead) > 1:                 # chunk N is written while chunk N+1 is analysed
                q, b, pa, f = ahead.popleft()
                yield q, b, pa, f if isinstance(f, Analysis) else f.result()
        while ahead:
            q, b, pa, f = ahead.popleft()
            yield q, b, pa, f if isinstance(f, Analysis) else f.result()

    try:
        for p, buf, parts, an in chunks():
            if cancel is not None and cancel.is_set():
                stop.set()
                raise CopyCancelled()
            if buf is not None and len(buf) != p.length:
                raise IOError(f"short read at {p.offset}: wanted {p.length}, got {len(buf)}")
            if parts is not None:              # the allocation map knows this chunk: only written parts move
                high, since_ckpt = _apply_sparse(p, buf, parts, an, stats=stats, writer=writer, hashes=hashes,
                                                 since_ckpt=since_ckpt)
            else:
                high, since_ckpt = _apply(p, buf, an, stats=stats, writer=writer, hashes=hashes, by_hash=by_hash,
                                          compare=compare, compare_block=compare_block, since_ckpt=since_ckpt)
            if on_progress:
                on_progress(high, size)
            if on_checkpoint and since_ckpt >= checkpoint_every:
                writer.flush()
                on_checkpoint(high)
                since_ckpt = 0
    finally:
        stop.set()
        pool.shutdown(wait=True, cancel_futures=True)
    writer.flush()
    if on_checkpoint:
        on_checkpoint(size)
    stats.seconds = time.monotonic() - t0
    return stats


def _apply_sparse(p: ByteRange, buf: bytes | None, parts: list[ByteRange], an: Analysis, *, stats: CopyStats,
                  writer: DiskWriter, hashes: BlockHashes | None, since_ckpt: int) -> tuple[int, int]:
    """A chunk the allocation map covers, onto a zeroed target: write only the written parts that hold data.
    Checksums come from the whole chunk (unwritten gaps are zeros, as on the target)."""
    read = sum(r.length for r in parts)
    stats.bytes_read += read
    stats.bytes_zero_skipped += p.length - read
    view = memoryview(buf) if buf is not None else None
    for r in parts:
        piece = view[r.offset - p.offset:r.end - p.offset]  # type: ignore[index]
        if _is_zero(piece):
            stats.bytes_zero_skipped += r.length
            continue
        writer.write(r.offset, piece)
        stats.bytes_written += r.length
    if hashes is not None:
        for k, o in enumerate(range(0, p.length, hashes.block)):
            hashes.set((p.offset + o) // hashes.block, an.digests[k])  # type: ignore[index]
    return p.end, since_ckpt + p.length


def _apply(p: ByteRange, buf: bytes, an: Analysis, *, stats: CopyStats, writer: DiskWriter,
           hashes: BlockHashes | None, by_hash: bool, compare: DiskReader | None, compare_block: int,
           since_ckpt: int) -> tuple[int, int]:
    """Write one chunk (or only what differs) and record its checksums. Returns (high watermark, bytes since
    the last checkpoint)."""
    stats.bytes_read += p.length
    if by_hash:
        # compare against the checksums recorded when each block was last written
        for k, o in enumerate(range(0, p.length, hashes.block)):  # type: ignore[union-attr]
            nb = buf[o:o + hashes.block]  # type: ignore[union-attr]
            i = (p.offset + o) // hashes.block  # type: ignore[union-attr]
            dg = an.digests[k]  # type: ignore[index]
            known = hashes.get(i)  # type: ignore[union-attr]
            if known is None and compare is not None:     # checksum unknown: read this block back
                old = compare.read(p.offset + o, len(nb))
                for so in range(0, len(nb), compare_block):   # write only the sub-blocks that differ
                    sb = nb[so:so + compare_block]
                    if old[so:so + compare_block] == sb:
                        stats.bytes_unchanged += len(sb)
                    else:
                        writer.write(p.offset + o + so, sb)
                        stats.bytes_written += len(sb)
                hashes.set(i, dg)  # type: ignore[union-attr]
                continue
            if known == dg:
                stats.bytes_unchanged += len(nb)
                hashes.set(i, dg)  # type: ignore[union-attr]
            else:
                writer.write(p.offset + o, nb)
                stats.bytes_written += len(nb)
                hashes.set(i, dg)  # type: ignore[union-attr]
    else:
        if compare is not None:
            old = compare.read(p.offset, p.length)
            if old == buf:
                stats.bytes_unchanged += p.length
            else:  # write only the sub-blocks that differ
                for o in range(0, p.length, compare_block):
                    nb = buf[o:o + compare_block]
                    if old[o:o + compare_block] == nb:
                        stats.bytes_unchanged += len(nb)
                    else:
                        writer.write(p.offset + o, nb)
                        stats.bytes_written += len(nb)
        elif writer.prezeroed and an.zero:
            stats.bytes_zero_skipped += p.length
        else:
            writer.write(p.offset, buf)
            stats.bytes_written += p.length
        if hashes is not None:        # record what the replica now holds, block by block
            for k, o in enumerate(range(0, p.length, hashes.block)):
                hashes.set((p.offset + o) // hashes.block, an.digests[k])  # type: ignore[index]
    return p.end, since_ckpt + p.length
