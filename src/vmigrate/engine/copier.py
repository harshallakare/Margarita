"""Purpose: the data-movement primitive — pipe DiskReader -> DiskWriter in large linear chunks with
zero-skipping, progress callbacks, resume-from-watermark and optional changed-range copy.

Usage:
    stats = copy_disk(reader, writer, chunk_size=64 << 20, start_offset=state.bytes_copied,
                      on_checkpoint=lambda off: state.save_watermark(off))

Compare mode (replica sync without source change tracking): pass `compare=<reader of the current
replica>`; every chunk is still read from the source, but only chunks that differ are written.

Resume contract: on_checkpoint(offset) is called only AFTER writer.flush(), so a persisted
watermark always means "everything below this offset is durable on the target".
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..core.interfaces import DiskReader, DiskWriter
from ..core.ranges import ByteRange, clip, split

DEFAULT_CHUNK = 64 << 20  # dd bs=64M — what worked on the reference path


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
) -> CopyStats:
    if writer.size < reader.size:
        raise ValueError(f"target disk ({writer.size} B) smaller than source ({reader.size} B)")
    size = reader.size
    if ranges is None:
        work = [ByteRange(start_offset, size - start_offset)] if start_offset < size else []
    else:
        work = [r for r in clip(ranges, size) if r.end > start_offset]
        work = [ByteRange(max(r.offset, start_offset), r.end - max(r.offset, start_offset)) for r in work]
    pieces = split(work, chunk_size)

    stats = CopyStats(bytes_total=size, resumed_from=start_offset)
    t0 = time.monotonic()
    since_ckpt = 0
    high = start_offset
    for p in pieces:
        if cancel is not None and cancel.is_set():
            raise CopyCancelled()
        buf = reader.read(p.offset, p.length)
        if len(buf) != p.length:
            raise IOError(f"short read at {p.offset}: wanted {p.length}, got {len(buf)}")
        stats.bytes_read += p.length
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
        elif writer.prezeroed and _is_zero(buf):
            stats.bytes_zero_skipped += p.length
        else:
            writer.write(p.offset, buf)
            stats.bytes_written += p.length
        high = p.end
        since_ckpt += p.length
        if on_progress:
            on_progress(high, size)
        if on_checkpoint and since_ckpt >= checkpoint_every:
            writer.flush()
            on_checkpoint(high)
            since_ckpt = 0
    writer.flush()
    if on_checkpoint:
        on_checkpoint(size)
    stats.seconds = time.monotonic() - t0
    return stats
