"""Purpose: read an Azure managed-disk snapshot through its read-only SAS URL (a page blob), list
its written pages, and diff two incremental snapshots of the same disk.

Usage:
    r = SasSnapshotReader(sas_source, size, http=http)     # sas_source(renew: bool) -> SAS URL
    r.read(off, n); r.allocated_ranges(); r.close()
    page_ranges(http, sas, size)                           # written pages
    page_ranges(http, sas, size, previous=base_sas)        # pages changed since base (Get Page Ranges diff)

Design:
  * reads are split into <= 4 MiB pieces fetched in parallel; each piece asks for its Content-MD5
    (x-ms-range-get-content-md5) and is re-fetched when the MD5 does not match
  * 429 / 5xx / connection drops back off and retry; a 403 means the SAS expired or was revoked:
    the reader asks for a new SAS once and continues
  * page ranges are listed in 64 GiB segments (Azure recommends splitting Get Page Ranges on large
    blobs) and follow NextMarker; never-written pages read as zeros and a fresh copy skips them
  * the diff (x-ms-previous-snapshot-url) returns changed pages AND cleared pages; both are changes
"""

from __future__ import annotations

import base64
import hashlib
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ...core.errors import ProviderError, TransientProviderError
from ...core.interfaces import DiskReader
from ...core.ranges import ByteRange, normalize
from .http import Http

VERSION = "2021-08-06"
PIECE = 4 << 20                 # largest range for which Azure returns a Content-MD5
SEGMENT = 64 << 30
RETRY = (408, 429, 500, 502, 503, 504)


def _with(url: str, extra: str) -> str:
    return url + ("&" if "?" in url else "?") + extra


def _call(http: Http, method: str, url: str, headers: dict[str, str], *, ok: tuple[int, ...], retries: int = 8,
          sleep: Any = time.sleep, timeout: float = 120) -> tuple[int, dict[str, str], bytes]:
    for attempt in range(retries):
        try:
            st, hdr, body = http(method, url, headers, None, timeout)
        except OSError as e:
            if attempt == retries - 1:
                raise TransientProviderError(f"Azure blob read: {e}") from e
            sleep(min(20.0, 0.5 * 2 ** attempt))
            continue
        if st in ok or st == 403:
            return st, hdr, body
        if st in RETRY and attempt < retries - 1:
            sleep(min(float(hdr.get("retry-after") or 0) or 0.5 * 2 ** attempt, 60.0))
            continue
        msg = f"Azure blob {method}: HTTP {st} {hdr.get('x-ms-error-code', '')} {body[:200].decode(errors='replace')}"
        raise (TransientProviderError if st in RETRY else ProviderError)(msg)
    raise AssertionError("unreachable")


def _parse_ranges(xml: bytes) -> tuple[list[ByteRange], str | None]:
    root = ET.fromstring(xml)
    out = []
    for el in root:
        if el.tag in ("PageRange", "ClearRange"):
            a, b = int(el.findtext("Start") or 0), int(el.findtext("End") or -1)
            if b >= a:
                out.append(ByteRange(a, b - a + 1))
    return out, (root.findtext("NextMarker") or None)


def page_ranges(http: Http, sas: str | Callable[[bool], str], size: int, *, previous: str | None = None,
                sleep: Any = time.sleep) -> list[ByteRange]:
    """Written pages of the snapshot, or with `previous` (the base snapshot's SAS URL) the pages
    that differ from it. `sas` may be a callable that renews the SAS on 403."""
    get_sas = sas if callable(sas) else (lambda renew: sas)
    out: list[ByteRange] = []
    for seg in range(0, size, SEGMENT):
        end = min(size, seg + SEGMENT) - 1
        marker: str | None = None
        renewed = False
        while True:
            h = {"x-ms-version": VERSION, "x-ms-range": f"bytes={seg}-{end}"}
            if previous:
                h["x-ms-previous-snapshot-url"] = previous
            url = _with(get_sas(False), "comp=pagelist" + (f"&marker={urllib.parse.quote(marker, safe='')}" if marker else ""))
            st, _, body = _call(http, "GET", url, h, ok=(200,), sleep=sleep)
            if st == 403:
                if renewed:
                    raise ProviderError("Azure Get Page Ranges: HTTP 403 with a fresh SAS")
                renewed = True
                get_sas(True)
                continue
            rs, marker = _parse_ranges(body)
            out += rs
            if not marker:
                break
    return normalize(r for r in out if r.offset < size)


class SasSnapshotReader(DiskReader):
    def __init__(self, sas_source: Callable[[bool], str], size: int, *, http: Http, workers: int = 16,
                 sleep: Any = time.sleep, on_close: Callable[[], None] | None = None):
        self._sas_source, self._size, self.http, self.sleep = sas_source, size, http, sleep
        self._sas = sas_source(False)
        self._lock = threading.Lock()
        self._on_close = on_close
        self._pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="azblob")
        st, hdr, _ = _call(http, "HEAD", self._sas, {"x-ms-version": VERSION}, ok=(200,), sleep=sleep)
        if st == 403:
            self._renew(self._sas)
            st, hdr, _ = _call(http, "HEAD", self._sas, {"x-ms-version": VERSION}, ok=(200,), sleep=sleep)
        blob = int(hdr.get("content-length") or 0)
        if st != 200 or blob < size:
            raise ProviderError(f"Azure snapshot blob is {blob} bytes, expected at least {size} (HTTP {st})")

    @property
    def size(self) -> int:
        return self._size

    def _renew(self, stale: str) -> str:
        with self._lock:
            if self._sas == stale:          # another thread may have renewed it already
                self._sas = self._sas_source(True)
            return self._sas

    def _piece(self, off: int, n: int) -> bytes:
        sas = self._sas
        for attempt in range(4):
            h = {"x-ms-version": VERSION, "x-ms-range": f"bytes={off}-{off + n - 1}",
                 "x-ms-range-get-content-md5": "true"}
            st, hdr, body = _call(self.http, "GET", sas, h, ok=(200, 206), sleep=self.sleep)
            if st == 403:
                if attempt >= 1:
                    raise ProviderError("Azure snapshot read: HTTP 403 with a fresh SAS (access revoked?)")
                sas = self._renew(sas)
                continue
            md5 = hdr.get("content-md5")
            if len(body) == n and (not md5 or base64.b64encode(hashlib.md5(body).digest()).decode() == md5):  # noqa: S324
                return body
        raise TransientProviderError(f"Azure snapshot read at {off}+{n}: short read or MD5 mismatch 3 times")

    def read(self, offset: int, length: int) -> bytes:
        length = max(0, min(length, self._size - offset))
        if length == 0:
            return b""
        jobs = [self._pool.submit(self._piece, o, min(PIECE, offset + length - o))
                for o in range(offset, offset + length, PIECE)]
        return b"".join(j.result() for j in jobs)

    def allocated_ranges(self) -> list[ByteRange]:
        return page_ranges(self.http, lambda renew: self._renew(self._sas) if renew else self._sas, self._size,
                           sleep=self.sleep)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        cb, self._on_close = self._on_close, None
        if cb:
            cb()
