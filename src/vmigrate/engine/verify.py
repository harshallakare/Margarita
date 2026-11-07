"""Purpose: post-copy verification — size match and deterministic sampled checksums comparing the
frozen source against what landed on the target (staged image and/or target read-back).

Usage:
    offsets = sample_offsets(disk_id, size, count=32, length=1 << 20)
    res = compare_samples(source_reader, target_reader, offsets, length=1 << 20)
    if not res.ok: raise VerificationError(res.detail)

Samples always include the first and last MiB (partition table / backup GPT live there) plus
seeded pseudo-random offsets, so re-runs check the same places.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field

from ..core.interfaces import DiskReader


@dataclass
class SampleResult:
    ok: bool
    checked: int
    mismatches: list[int] = field(default_factory=list)
    detail: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "checked": self.checked, "mismatches": self.mismatches, "detail": self.detail}


def sample_offsets(seed: str, size: int, *, count: int = 32, length: int = 1 << 20) -> list[int]:
    if size <= length:
        return [0]
    last = size - length
    rng = random.Random(f"vmigrate:{seed}:{size}")
    offs = {0, last}
    while len(offs) < min(count, max(2, size // length)):
        offs.add(rng.randrange(0, last // 4096 + 1) * 4096)
    return sorted(offs)


def _digest(reader: DiskReader, off: int, length: int) -> str:
    return hashlib.sha256(reader.read(off, min(length, reader.size - off))).hexdigest()


def compare_samples(src: DiskReader, dst: DiskReader, offsets: list[int], *, length: int = 1 << 20) -> SampleResult:
    bad = [o for o in offsets if _digest(src, o, length) != _digest(dst, o, length)]
    return SampleResult(
        ok=not bad,
        checked=len(offsets),
        mismatches=bad,
        detail="all samples match" if not bad else f"{len(bad)}/{len(offsets)} sampled regions differ",
    )


def check_size(source_size: int, target_size: int) -> tuple[bool, str]:
    if target_size < source_size:
        return False, f"target {target_size} B < source {source_size} B"
    if target_size > source_size:
        return True, f"target rounded up by {target_size - source_size} B (platform granularity)"
    return True, "exact size match"
