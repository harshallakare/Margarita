"""Purpose: an allocation map that is filled IN THE BACKGROUND while a disk is copied, so a full copy can skip
never-written space without ever waiting for the map.

Seen live (Nutanix CRT on a 10 TiB disk): the map scans at ~1.6 GiB/s with ~250 regions per GiB; waiting for
it delayed every disk by up to the time budget. Now the copy starts at once; each chunk asks the tracker:
  * None          -> the map has not reached this chunk yet: read it all (exactly as without a map)
  * []            -> covered, nothing written there: skip it (the fresh target already reads zeros)
  * [ranges...]   -> covered, read and write only these parts (aligned to `align`, clipped to the chunk)

Usage:
    t = AllocationTracker(size, align=1 << 20)
    producer thread: t.add([(off, len), ...], covered_to); ...; t.finish()   (or t.fail(err))
    copier:          t.parts(offset, length)
    t.close()        # copy done: tells the producer to stop
"""

from __future__ import annotations

import bisect
import threading

from .ranges import ByteRange


class AllocationTracker:
    def __init__(self, size: int, *, align: int = 1 << 20):
        self.size, self.align = size, max(1, align)
        self._starts: list[int] = []
        self._ends: list[int] = []
        self._covered = 0
        self._lock = threading.Lock()
        self.closed = threading.Event()
        self.error: str | None = None
        self.written_bytes = 0                  # allocated bytes seen so far (for logs)

    @property
    def covered(self) -> int:
        return self._covered

    def add(self, regions: list[tuple[int, int]], covered_to: int) -> None:
        """Regions [(offset, length)] in increasing order, all below covered_to (one map page)."""
        with self._lock:
            for off, n in regions:
                if n <= 0 or off >= self.size:
                    continue
                a = (off // self.align) * self.align
                b = min(self.size, -(-(off + n) // self.align) * self.align)
                if self._ends and a <= self._ends[-1]:
                    if b > self._ends[-1]:
                        self.written_bytes += b - self._ends[-1]
                        self._ends[-1] = b
                else:
                    self._starts.append(a)
                    self._ends.append(b)
                    self.written_bytes += b - a
            self._covered = max(self._covered, min(self.size, covered_to))

    def finish(self) -> None:
        with self._lock:
            self._covered = self.size

    def fail(self, error: str) -> None:
        self.error = error                       # coverage stops growing: later chunks are read in full

    def close(self) -> None:
        self.closed.set()

    def parts(self, offset: int, length: int) -> list[ByteRange] | None:
        end = offset + length
        with self._lock:
            if end > self._covered:
                return None
            i = max(0, bisect.bisect_right(self._starts, offset) - 1)
            out = []
            while i < len(self._starts) and self._starts[i] < end:
                a, b = max(self._starts[i], offset), min(self._ends[i], end)
                if b > a:
                    out.append(ByteRange(a, b - a))
                i += 1
            return out
