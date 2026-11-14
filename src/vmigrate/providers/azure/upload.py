"""Purpose: write a disk into an Azure managed disk created for upload (createOption Upload): Put Page calls on
the disk's write SAS (512-byte aligned ranges of up to 4 MiB, in parallel), then the 512-byte fixed-VHD footer.

Usage:
    w = PageBlobWriter(http, write_sas, size_bytes)       # DiskWriter; the disk is uploadSizeBytes = size + 512
    w.write(off, data); w.close()
    put_footer(http, write_sas, size_bytes)              # before endGetAccess

Facts (Microsoft "Upload a VHD to Azure" / Put Page REST reference): an upload disk reads as zeros where no page
was put, so zero chunks are skipped; pages are 512-byte aligned, at most 4 MiB per call; the last 512 bytes are a
fixed VHD footer; the upload ends with endGetAccess (the disk then becomes Unattached and is final).
"""

from __future__ import annotations

import base64
import hashlib
import struct
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from ...core.errors import ProviderError, TransientProviderError
from ...core.formats.vhd import vhd_checksum
from ...core.interfaces import DiskWriter
from ..common.http import Http

PAGE = 512
MAX_PUT = 4 << 20
VERSION = "2021-08-06"
RETRY = (408, 429, 500, 502, 503, 504)


def _with(url: str, extra: str) -> str:
    return url + ("&" if "?" in url else "?") + extra


def put_pages(http: Http, sas: str, offset: int, data: bytes, *, retries: int = 8, sleep: Any = time.sleep) -> None:
    if offset % PAGE or len(data) % PAGE or not data or len(data) > MAX_PUT:
        raise ProviderError(f"Azure Put Page: range {offset}+{len(data)} is not 512-aligned / at most 4 MiB")
    h = {"x-ms-version": VERSION, "x-ms-page-write": "update", "x-ms-range": f"bytes={offset}-{offset + len(data) - 1}",
         "Content-Length": str(len(data)), "Content-MD5": base64.b64encode(hashlib.md5(data).digest()).decode()}  # noqa: S324
    url = _with(sas, "comp=page")
    for attempt in range(retries):
        try:
            st, hdr, body = http("PUT", url, h, data, 120)
        except OSError as e:
            if attempt == retries - 1:
                raise TransientProviderError(f"Azure Put Page at {offset}: {e}") from e
            sleep(min(20.0, 0.5 * 2 ** attempt))
            continue
        if st == 201:
            return
        if st in RETRY and attempt < retries - 1:
            sleep(min(float(hdr.get("retry-after") or 0) or 0.5 * 2 ** attempt, 60.0))
            continue
        msg = f"Azure Put Page at {offset}: HTTP {st} {hdr.get('x-ms-error-code', '')} {body[:200]!r}"
        raise (TransientProviderError if st in RETRY else ProviderError)(msg)


def chs(size: int) -> tuple[int, int, int]:
    """CHS geometry of the VHD specification (Appendix: CHS calculation)."""
    sectors = min(size // 512, 65535 * 16 * 255)
    if sectors >= 65535 * 16 * 63:
        spt, heads = 255, 16
        cth = sectors // spt
    else:
        spt = 17
        cth = sectors // spt
        heads = max(4, (cth + 1023) // 1024)
        if cth >= heads * 1024 or heads > 16:
            spt, heads = 31, 16
            cth = sectors // spt
        if cth >= heads * 1024:
            spt, heads = 63, 16
            cth = sectors // spt
    return cth // heads, heads, spt


def vhd_footer(size: int, *, ident: bytes | None = None, now: float | None = None) -> bytes:
    """512-byte fixed-disk VHD footer for a disk of `size` bytes."""
    c, h, s = chs(size)
    stamp = int((now if now is not None else time.time()) - 946684800) & 0xFFFFFFFF   # seconds since 2000-01-01
    f = bytearray(512)
    f[0:8] = b"conectix"
    struct.pack_into(">I", f, 8, 2)                       # features: reserved bit set
    struct.pack_into(">I", f, 12, 0x00010000)             # format version 1.0
    struct.pack_into(">Q", f, 16, 0xFFFFFFFFFFFFFFFF)     # data offset: fixed disk
    struct.pack_into(">I", f, 24, stamp)
    f[28:32] = b"vmig"
    struct.pack_into(">I", f, 32, 0x00010000)
    f[36:40] = b"Wi2k"
    struct.pack_into(">QQ", f, 40, size, size)            # original size, current size
    struct.pack_into(">HBB", f, 56, c, h, s)
    struct.pack_into(">I", f, 60, 2)                      # disk type: fixed
    f[68:84] = ident or uuid.uuid4().bytes
    struct.pack_into(">I", f, 64, vhd_checksum(bytes(f)))
    return bytes(f)


def put_footer(http: Http, sas: str, size: int, **kw: Any) -> None:
    put_pages(http, sas, size, vhd_footer(size), **kw)


class PageBlobWriter(DiskWriter):
    def __init__(self, http: Http, sas: str, size: int, *, workers: int = 8, sleep: Any = time.sleep):
        if size % (1 << 20):
            raise ProviderError(f"Azure upload disk size {size} must be a whole number of MiB")
        self.http, self.sas, self._size, self.sleep = http, sas, size, sleep
        self.prezeroed = True                 # an upload disk reads as zeros where nothing was put
        self.resumable = False                # the write SAS is gone after a restart; the disk is recreated
        self._pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="azput")
        self._futs: list[Future] = []
        self._lock = threading.Lock()
        self._closed = False

    @property
    def size(self) -> int:
        return self._size

    def write(self, offset: int, buf: bytes) -> None:
        if offset + len(buf) > self._size:
            raise ValueError(f"write past end: {offset}+{len(buf)} > {self._size}")
        view = memoryview(buf)
        for pos in range(0, len(buf), MAX_PUT):
            chunk = bytes(view[pos:pos + MAX_PUT])
            with self._lock:
                for f in [f for f in self._futs if f.done()]:
                    self._futs.remove(f)
                    f.result()
                while len(self._futs) >= 16:
                    self._futs.pop(0).result()
                self._futs.append(self._pool.submit(put_pages, self.http, self.sas, offset + pos, chunk,
                                                    sleep=self.sleep))

    def flush(self) -> None:
        with self._lock:
            futs, self._futs = self._futs, []
        for f in futs:
            f.result()

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.flush()
        finally:
            self._closed = True
            self._pool.shutdown(wait=True)
