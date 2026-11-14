"""Purpose: write a disk INTO a new EBS snapshot with the EBS direct APIs (StartSnapshot -> PutSnapshotBlock ->
CompleteSnapshot): no helper instance, no volume attach, works from any worker with HTTPS to AWS.

Usage:
    sid = start_snapshot(ebs, size_gib, tags, parent=None)          # parent: incremental (replica sync)
    w = EbsSnapshotWriter(ebs, sid, size, parent_reader=None)       # DiskWriter
    w.write(off, data); w.flush(); n = w.close(); complete_snapshot(ebs, sid, n)

Design:
  * EBS blocks are 512 KiB. Whole blocks are put as soon as they are written (in parallel, SHA-256 checksum).
  * A partly written block is kept in memory until it is complete; flush() completes it from the parent
    snapshot (incremental) or with zeros (new snapshot: never-written blocks read as zeros) and puts it.
    Putting a block index twice is allowed while the snapshot is pending (the last put wins).
  * A new snapshot reads as zeros where nothing was put, so the copier skips zero chunks (prezeroed);
    an incremental one inherits the parent, so zeros are written explicitly.
"""

from __future__ import annotations

import base64
import hashlib
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from ...core.errors import ProviderError
from ...core.interfaces import DiskReader, DiskWriter
from .ebs import call

BLOCK = 512 << 10


def start_snapshot(ebs: Any, size_gib: int, tags: dict[str, str], *, parent: str | None = None,
                   timeout_minutes: int = 120, description: str = "", sleep: Any = time.sleep) -> str:
    kw: dict[str, Any] = {"VolumeSize": size_gib, "Timeout": timeout_minutes, "Description": description[:255],
                          "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}
    if parent:
        kw["ParentSnapshotId"] = parent
    return str(call(ebs.start_snapshot, sleep=sleep, **kw)["SnapshotId"])


def complete_snapshot(ebs: Any, snapshot_id: str, changed_blocks: int, *, sleep: Any = time.sleep) -> str:
    return str(call(ebs.complete_snapshot, sleep=sleep, SnapshotId=snapshot_id, ChangedBlocksCount=changed_blocks)
               .get("Status", ""))


class EbsSnapshotWriter(DiskWriter):
    def __init__(self, ebs: Any, snapshot_id: str, size: int, *, parent_reader: DiskReader | None = None,
                 workers: int = 16, sleep: Any = time.sleep):
        if size % BLOCK:
            raise ProviderError(f"EBS snapshot size {size} is not a multiple of 512 KiB")
        self.ebs, self.snap, self._size, self.sleep = ebs, snapshot_id, size, sleep
        self.parent = parent_reader
        self.prezeroed = parent_reader is None
        self.resumable = False                  # a reopened writer is a new snapshot
        self._pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="ebs-put")
        self._futs: list[Future] = []
        self._partial: dict[int, tuple[bytearray, list[tuple[int, int]]]] = {}
        self._blocks: set[int] = set()
        self._lock = threading.Lock()
        self._closed = False

    @property
    def size(self) -> int:
        return self._size

    def _put(self, idx: int, data: bytes) -> None:
        digest = base64.b64encode(hashlib.sha256(data).digest()).decode()
        call(self.ebs.put_snapshot_block, sleep=self.sleep, SnapshotId=self.snap, BlockIndex=idx, BlockData=data,
             DataLength=BLOCK, Checksum=digest, ChecksumAlgorithm="SHA256")

    def _submit(self, idx: int, data: bytes) -> None:
        with self._lock:
            self._blocks.add(idx)
            self._futs = [f for f in self._futs if not f.done() or f.exception() is not None]
            for f in self._futs:
                if f.done() and f.exception() is not None:
                    raise f.exception()  # type: ignore[misc]
            while len(self._futs) > 64:                  # bound the memory held by queued puts (~32 MiB)
                self._futs.pop(0).result()
            self._futs.append(self._pool.submit(self._put, idx, bytes(data)))

    def write(self, offset: int, buf: bytes) -> None:
        if offset + len(buf) > self._size:
            raise ValueError(f"write past end: {offset}+{len(buf)} > {self._size}")
        view, pos = memoryview(buf), 0
        while pos < len(buf):
            off = offset + pos
            idx, inner = divmod(off, BLOCK)
            n = min(BLOCK - inner, len(buf) - pos)
            if inner == 0 and n == BLOCK and idx not in self._partial:
                self._submit(idx, view[pos:pos + n])
            else:
                data, cov = self._partial.setdefault(idx, (bytearray(BLOCK), []))
                data[inner:inner + n] = view[pos:pos + n]
                cov.append((inner, inner + n))
                if _covered(cov):
                    self._partial.pop(idx)
                    self._submit(idx, data)
            pos += n

    def _complete_partials(self) -> None:
        for idx in sorted(self._partial):
            data, cov = self._partial.pop(idx)
            if self.parent is not None:
                base = bytearray(self.parent.read(idx * BLOCK, BLOCK))
                for a, b in cov:
                    base[a:b] = data[a:b]
                data = base
            self._submit(idx, data)

    def flush(self) -> None:
        self._complete_partials()
        with self._lock:
            futs, self._futs = self._futs, []
        for f in futs:
            f.result()

    @property
    def changed_blocks(self) -> int:
        return len(self._blocks)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.flush()
        finally:
            self._closed = True
            self._pool.shutdown(wait=True)


def _covered(cov: list[tuple[int, int]]) -> bool:
    end = 0
    for a, b in sorted(cov):
        if a > end:
            return False
        end = max(end, b)
    return end >= BLOCK
