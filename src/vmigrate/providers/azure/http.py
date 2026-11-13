"""Purpose: the HTTP layer of the Azure source, with no Azure SDK: one callable that the ARM client
and the blob reader share and that tests replace with a fake.

    Http = (method, url, headers, body | None, timeout) -> (status, headers with lower-case keys, body)

The default keeps one HTTPS connection per thread and host open (a disk copy makes thousands of
4 MiB range reads from the same storage account, and a new TLS handshake for each one costs about
as much as the transfer). When an HTTPS proxy is configured in the environment it falls back to
urllib, which honours HTTPS_PROXY / NO_PROXY.

Usage:
    http = default_http()
    status, headers, body = http("GET", url, {"x-ms-version": "2021-08-06"}, None, 60)
"""

from __future__ import annotations

import http.client
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

Http = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, dict[str, str], bytes]]


def _urllib(method: str, url: str, headers: dict[str, str], body: bytes | None,
            timeout: float) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - https endpoints only
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read()


class KeepAliveHttp:
    """Per-thread persistent HTTPS connections. A connection that fails is dropped and the request
    is sent once more on a new one (the server may have closed an idle connection)."""

    def __init__(self) -> None:
        self._local = threading.local()

    def _conn(self, host: str, timeout: float, fresh: bool) -> http.client.HTTPSConnection:
        pool = getattr(self._local, "pool", None)
        if pool is None:
            pool = self._local.pool = {}
        c = pool.get(host)
        if fresh and c is not None:
            c.close()
            c = None
        if c is None:
            c = pool[host] = http.client.HTTPSConnection(host, timeout=timeout)
        return c

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None,
                 timeout: float) -> tuple[int, dict[str, str], bytes]:
        u = urllib.parse.urlsplit(url)
        if u.scheme != "https":
            return _urllib(method, url, headers, body, timeout)
        path = u.path + ("?" + u.query if u.query else "")
        for attempt in (0, 1):
            c = self._conn(u.netloc, timeout, fresh=attempt == 1)
            try:
                c.request(method, path, body=body, headers=headers)
                r = c.getresponse()
                data = r.read()
                return r.status, {k.lower(): v for k, v in r.getheaders()}, data
            except (http.client.HTTPException, ConnectionError, TimeoutError, OSError):
                c.close()
                self._local.pool.pop(u.netloc, None)
                if attempt == 1:
                    raise
        raise AssertionError("unreachable")


def default_http() -> Http:
    return _urllib if urllib.request.getproxies().get("https") else KeepAliveHttp()
