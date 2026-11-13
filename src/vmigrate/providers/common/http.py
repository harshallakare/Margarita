"""Purpose: a small HTTP layer for REST-only providers (Azure, oVirt, ...): one callable that the
API client and the disk reader share and that tests replace with a fake.

    Http = (method, url, headers, body | None, timeout) -> (status, headers with lower-case keys, body)

The default keeps one HTTPS connection per thread and host open (a disk copy makes thousands of
range reads from the same server, and a new TLS handshake for each one costs about
as much as the transfer). When an HTTPS proxy is configured in the environment it falls back to
urllib, which honours HTTPS_PROXY / NO_PROXY.

Usage:
    http = default_http()                          # or default_http(ssl_context) to pin a CA
    status, headers, body = http("GET", url, {"x-ms-version": "2021-08-06"}, None, 60)
"""

from __future__ import annotations

import http.client
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

Http = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, dict[str, str], bytes]]


def _urllib(method: str, url: str, headers: dict[str, str], body: bytes | None,
            timeout: float, context: ssl.SSLContext | None = None) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as r:  # noqa: S310 - https endpoints
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read()


class KeepAliveHttp:
    """Per-thread persistent HTTPS connections. A connection that fails is dropped and the request
    is sent once more on a new one (the server may have closed an idle connection)."""

    def __init__(self, context: ssl.SSLContext | None = None) -> None:
        self._local = threading.local()
        self.context = context

    def _conn(self, host: str, timeout: float, fresh: bool) -> http.client.HTTPSConnection:
        pool = getattr(self._local, "pool", None)
        if pool is None:
            pool = self._local.pool = {}
        c = pool.get(host)
        if fresh and c is not None:
            c.close()
            c = None
        if c is None:
            c = pool[host] = http.client.HTTPSConnection(host, timeout=timeout, context=self.context)
        return c

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None,
                 timeout: float) -> tuple[int, dict[str, str], bytes]:
        u = urllib.parse.urlsplit(url)
        if u.scheme != "https":
            return _urllib(method, url, headers, body, timeout, self.context)
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


def default_http(context: ssl.SSLContext | None = None) -> Http:
    if urllib.request.getproxies().get("https"):
        return lambda m, u, h, b, t: _urllib(m, u, h, b, t, context)
    return KeepAliveHttp(context)


def tls_context(verify: bool = True, ca_pem: str | None = None) -> ssl.SSLContext:
    """Verification on by default; ca_pem pins a private CA (e.g. the oVirt engine CA)."""
    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context(cadata=ca_pem) if ca_pem and ca_pem.strip() else ssl.create_default_context()
