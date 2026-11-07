"""Purpose: pure byte-range math used by the copier and by incremental (changed-region) sync.

Usage:
    r = ByteRange(offset, length)
    normalize(ranges)                 # sort + merge overlapping/adjacent ranges
    align(ranges, 1 << 20, size)      # expand to block alignment, clip to disk size
    split(ranges, chunk)              # cut into <= chunk pieces for the copier
    total(ranges); subtract(a, b)
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class ByteRange:
    offset: int
    length: int

    def __post_init__(self) -> None:
        if self.offset < 0 or self.length < 0:
            raise ValueError(f"invalid range {self.offset}+{self.length}")

    @property
    def end(self) -> int:
        return self.offset + self.length


def normalize(ranges: Iterable[ByteRange]) -> list[ByteRange]:
    out: list[ByteRange] = []
    for r in sorted(x for x in ranges if x.length > 0):
        if out and r.offset <= out[-1].end:
            last = out[-1]
            out[-1] = ByteRange(last.offset, max(last.end, r.end) - last.offset)
        else:
            out.append(r)
    return out


def clip(ranges: Iterable[ByteRange], size: int) -> list[ByteRange]:
    out = []
    for r in ranges:
        if r.offset >= size:
            continue
        out.append(ByteRange(r.offset, min(r.end, size) - r.offset))
    return normalize(out)


def align(ranges: Iterable[ByteRange], block: int, size: int) -> list[ByteRange]:
    if block <= 0:
        raise ValueError("block must be positive")
    out = []
    for r in ranges:
        start = (r.offset // block) * block
        end = -(-r.end // block) * block
        out.append(ByteRange(start, end - start))
    return clip(out, size)


def split(ranges: Iterable[ByteRange], chunk: int) -> list[ByteRange]:
    if chunk <= 0:
        raise ValueError("chunk must be positive")
    out = []
    for r in normalize(ranges):
        off = r.offset
        while off < r.end:
            n = min(chunk, r.end - off)
            out.append(ByteRange(off, n))
            off += n
    return out


def total(ranges: Iterable[ByteRange]) -> int:
    return sum(r.length for r in normalize(ranges))


def subtract(a: Iterable[ByteRange], b: Iterable[ByteRange]) -> list[ByteRange]:
    """Ranges in a that are not covered by b."""
    bs = normalize(b)
    out: list[ByteRange] = []
    for r in normalize(a):
        cur = r.offset
        for x in bs:
            if x.end <= cur or x.offset >= r.end:
                continue
            if x.offset > cur:
                out.append(ByteRange(cur, x.offset - cur))
            cur = max(cur, x.end)
            if cur >= r.end:
                break
        if cur < r.end:
            out.append(ByteRange(cur, r.end - cur))
    return out
