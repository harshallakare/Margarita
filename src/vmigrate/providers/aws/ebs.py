"""Purpose: read an EBS snapshot through the EBS direct APIs (no helper instance, no volume attach).

Usage:
    r = EbsSnapshotReader(ebs_client, "snap-0abc", size_bytes)     # ebs_client = boto3.client("ebs")
    r.read(off, n); r.allocated_ranges()
    changed_block_indices(ebs_client, "snap-old", "snap-new")      # ListChangedBlocks

Design:
  * ListSnapshotBlocks is consumed as a sliding WINDOW (StartingBlockIndex + MaxResults), never
    fully materialized: a 16 TiB volume has ~33M blocks and their tokens would not fit in memory.
    Tokens are re-listed when the window expires (ExpiryTime) or the read moves past it.
  * blocks absent from the listing were never written -> read as zeros (and are skipped entirely
    by a fresh full copy via allocated_ranges())
  * each read fetches its blocks with GetSnapshotBlock in parallel (bounded), verifies the SHA-256
    checksum of every block, and retries throttling / 5xx with exponential backoff
"""

from __future__ import annotations

import base64
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from ...core.errors import ProviderError, TransientProviderError
from ...core.interfaces import DiskReader
from ...core.ranges import ByteRange
from .mapping import blocks_to_ranges

THROTTLE = ("ThrottlingException", "RequestThrottledException", "TooManyRequestsException",
            "ServiceUnavailableException", "InternalServerException", "SlowDown")


def _code(e: Exception) -> str:
    return str(getattr(e, "response", {}).get("Error", {}).get("Code", "")) if hasattr(e, "response") else ""


def call(fn: Any, *, retries: int = 8, sleep: Any = time.sleep, **kw: Any) -> Any:
    for attempt in range(retries):
        try:
            return fn(**kw)
        except Exception as e:  # noqa: BLE001 - classify below
            if _code(e) in THROTTLE and attempt < retries - 1:
                sleep(min(20.0, 0.2 * 2 ** attempt))
                continue
            if _code(e) in THROTTLE:
                raise TransientProviderError(f"EBS direct API throttled: {e}") from e
            raise ProviderError(f"EBS direct API: {e}") from e
    raise AssertionError("unreachable")


def _expired(expiry: Any) -> bool:
    if expiry is None:
        return False
    if isinstance(expiry, datetime):
        return datetime.now(timezone.utc) >= expiry.replace(tzinfo=expiry.tzinfo or timezone.utc)
    return False


class EbsSnapshotReader(DiskReader):
    def __init__(self, ebs: Any, snapshot_id: str, size: int, *, window: int = 10000, workers: int = 16,
                 sleep: Any = time.sleep):
        self.ebs, self.snap, self._size = ebs, snapshot_id, size
        self.window, self.sleep = max(1, min(window, 10000)), sleep   # AWS: MaxResults 100..10000
        self._pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="ebs")
        first = self._list(0)
        self.block_size = int(first.get("BlockSize") or 512 << 10)
        vol = int(first.get("VolumeSize") or 0) << 30
        if vol and vol < size:
            raise ProviderError(f"{snapshot_id}: snapshot volume size {vol} < expected {size}")
        self._win_start, self._win_end, self._tokens, self._expiry = 0, -1, {}, None
        self._load(0, first)

    def _list(self, start: int, next_token: str | None = None) -> dict[str, Any]:
        kw: dict[str, Any] = {"SnapshotId": self.snap, "StartingBlockIndex": start, "MaxResults": self.window}
        if next_token:
            kw = {"SnapshotId": self.snap, "NextToken": next_token, "MaxResults": self.window}
        return call(self.ebs.list_snapshot_blocks, sleep=self.sleep, **kw)

    def _load(self, start: int, page: dict[str, Any] | None = None) -> None:
        page = page or self._list(start)
        blocks = page.get("Blocks") or []
        self._tokens = {b["BlockIndex"]: b["BlockToken"] for b in blocks}
        self._win_start = start
        full = len(blocks) >= self.window and page.get("NextToken")
        self._win_end = blocks[-1]["BlockIndex"] if full else (self._size - 1) // self.block_size
        self._expiry = page.get("ExpiryTime")

    def _token(self, i: int) -> str | None:
        if not (self._win_start <= i <= self._win_end) or _expired(self._expiry):
            self._load(i)
        return self._tokens.get(i)

    @property
    def size(self) -> int:
        return self._size

    def _fetch(self, i: int, token: str) -> bytes:
        for attempt in range(3):
            r = call(self.ebs.get_snapshot_block, sleep=self.sleep, SnapshotId=self.snap, BlockIndex=i, BlockToken=token)
            data = r["BlockData"].read()
            if r.get("ChecksumAlgorithm", "SHA256") != "SHA256" or not r.get("Checksum") or \
                    base64.b64encode(hashlib.sha256(data).digest()).decode() == r["Checksum"]:
                break
            if attempt == 2:
                raise TransientProviderError(f"{self.snap} block {i}: SHA-256 checksum mismatch 3 times")
        if len(data) != self.block_size and (i + 1) * self.block_size <= self._size:
            raise ProviderError(f"{self.snap} block {i}: got {len(data)} bytes, expected {self.block_size}")
        return data

    def read(self, offset: int, length: int) -> bytes:
        length = max(0, min(length, self._size - offset))
        if length == 0:
            return b""
        first, last = offset // self.block_size, (offset + length - 1) // self.block_size
        jobs = {}
        for i in range(first, last + 1):
            t = self._token(i)
            if t is not None:
                jobs[i] = self._pool.submit(self._fetch, i, t)
        zero = bytes(self.block_size)
        buf = b"".join(jobs[i].result() if i in jobs else zero for i in range(first, last + 1))
        start = offset - first * self.block_size
        return buf[start:start + length]

    def allocated_ranges(self) -> list[ByteRange]:
        idx: list[int] = []
        page = self._list(0)
        while True:
            idx += [b["BlockIndex"] for b in page.get("Blocks") or []]
            if not page.get("NextToken"):
                break
            page = self._list(0, page["NextToken"])
        return blocks_to_ranges(idx, self.block_size, self._size)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def changed_block_indices(ebs: Any, first: str, second: str, *, sleep: Any = time.sleep) -> tuple[list[int], int]:
    """Block indices that differ between two snapshots of the same volume (incl. blocks zeroed in
    the second: no SecondBlockToken -> still changed, read back as zeros)."""
    idx: list[int] = []
    kw: dict[str, Any] = {"FirstSnapshotId": first, "SecondSnapshotId": second, "MaxResults": 10000}
    block = 512 << 10
    while True:
        page = call(ebs.list_changed_blocks, sleep=sleep, **kw)
        block = int(page.get("BlockSize") or block)
        idx += [b["BlockIndex"] for b in page.get("ChangedBlocks") or []]
        if not page.get("NextToken"):
            return idx, block
        kw = {"FirstSnapshotId": first, "SecondSnapshotId": second, "MaxResults": 10000, "NextToken": page["NextToken"]}
