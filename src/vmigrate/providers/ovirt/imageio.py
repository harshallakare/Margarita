"""Purpose: read a disk of an oVirt backup through an ImageIO transfer URL (HTTPS on the host,
port 54322, or the engine proxy on 54323) and list its extents.

Usage:
    r = ImageioReader(transfer_url, size, http=http, workers=8, on_close=finalize)
    r.read(off, n); r.allocated_ranges(); r.close()
    extents(http, transfer_url, "dirty")          # [ByteRange] changed since the backup's from_checkpoint

ImageIO API: GET <url> with a Range header -> 206; GET <url>/extents?context=zero ->
[{start, length, zero, hole}]; context=dirty (incremental backups) -> [{start, length, dirty, zero}].
Reads are split into 8 MiB requests fetched in parallel; 5xx and dropped connections are retried.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ...core.errors import ProviderError, TransientProviderError
from ...core.interfaces import DiskReader
from ...core.ranges import ByteRange, normalize
from ..common.http import Http

PIECE = 8 << 20
RETRY = (500, 502, 503, 504)


def _get(http: Http, url: str, headers: dict[str, str], ok: tuple[int, ...], sleep: Any, retries: int = 6) -> bytes:
    for attempt in range(retries):
        try:
            st, _, body = http("GET", url, headers, None, 300)
        except OSError as e:
            if attempt == retries - 1:
                raise TransientProviderError(f"ImageIO GET: {e}") from e
            sleep(min(20.0, 0.5 * 2 ** attempt))
            continue
        if st in ok:
            return body
        if st in RETRY and attempt < retries - 1:
            sleep(min(20.0, 0.5 * 2 ** attempt))
            continue
        msg = f"ImageIO GET {url.split('?')[0].rsplit('/', 1)[0]}/...: HTTP {st} {body[:200].decode(errors='replace')}"
        raise (TransientProviderError if st in RETRY else ProviderError)(msg)
    raise AssertionError("unreachable")


def extents(http: Http, url: str, context: str, size: int, *, sleep: Any = time.sleep) -> list[ByteRange]:
    """context "zero": ranges holding data; context "dirty": ranges changed since the checkpoint."""
    rows = json.loads(_get(http, f"{url.rstrip('/')}/extents?context={context}", {}, (200,), sleep) or b"[]")
    if context == "dirty":
        keep = [r for r in rows if r.get("dirty")]
    else:
        keep = [r for r in rows if not r.get("zero")]
    return normalize(ByteRange(int(r["start"]), min(int(r["length"]), size - int(r["start"])))
                     for r in keep if int(r["start"]) < size)


class ImageioReader(DiskReader):
    def __init__(self, url: str, size: int, *, http: Http, workers: int = 8, sleep: Any = time.sleep,
                 on_close: Callable[[], None] | None = None):
        self.url, self._size, self.http, self.sleep = url, size, http, sleep
        self._on_close = on_close
        self._pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="imageio")

    @property
    def size(self) -> int:
        return self._size

    def _piece(self, off: int, n: int) -> bytes:
        body = _get(self.http, self.url, {"Range": f"bytes={off}-{off + n - 1}"}, (200, 206), self.sleep)
        if len(body) != n:
            raise TransientProviderError(f"ImageIO short read at {off}: {len(body)} of {n} bytes")
        return body

    def read(self, offset: int, length: int) -> bytes:
        length = max(0, min(length, self._size - offset))
        if length == 0:
            return b""
        jobs = [self._pool.submit(self._piece, o, min(PIECE, offset + length - o))
                for o in range(offset, offset + length, PIECE)]
        return b"".join(j.result() for j in jobs)

    def allocated_ranges(self) -> list[ByteRange] | None:
        try:
            return extents(self.http, self.url, "zero", self._size, sleep=self.sleep)
        except ProviderError:
            return None

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        cb, self._on_close = self._on_close, None
        if cb:
            cb()
