"""Purpose: client for Cloudbase's rct-service (Apache-2.0, https://github.com/cloudbase/rct-service),
a Rust REST service run on the Hyper-V host that exposes virtual-disk info, RCT changed ranges and
disk content over HTTPS. With it the worker needs no SMB/CSV share mounts.

Usage (Hyper-V endpoint config):
    rct_service_url: https://hv01.example.com:6677
    rct_service_auth_key: <secret, stored encrypted>         # the service's Rocket.toml auth_key
    rct_service_fingerprint: <sha256 hex of its certificate>  # pinned (the service uses a self-signed cert)

API used (as documented by the service; every request carries the `auth_key` header):
    GET  /vdisk/<path>/info                     {"virtual_size", "parent_path", ...}
    GET  /vdisk/<path>/rct/<rct_id>/changes     [{"offset", "length"}]   (bytes)
    POST /vdisk/<path>/content                  body [{"offset","length"}] -> raw bytes, concatenated
<path> is the Windows path as ONE URL segment (percent-encoded). UNVERIFIED against a live service:
the service's handling of encoded backslashes/colons is not documented.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from ...core.errors import ConfigError, ProviderError, TransientProviderError
from ...core.interfaces import DiskReader
from ...core.ranges import ByteRange


class RctServiceClient:
    def __init__(self, url: str, auth_key: str, *, fingerprint: str | None = None, http: Any = None,
                 timeout: float = 600):
        if not url.startswith("https://") and http is None:
            raise ConfigError("hyperv: rct_service_url must be https:// (disk data and the auth key cross the network)")
        if not auth_key:
            raise ConfigError("hyperv: rct_service_auth_key is required with rct_service_url")
        if http is None:
            from ...export.remote import _session
            http = _session(fingerprint)
        self.url, self.key, self.http, self.timeout = url.rstrip("/"), auth_key, http, timeout
        self.verify = False if fingerprint else True

    def _req(self, method: str, path: str, *, body: Any = None) -> Any:
        kw: dict[str, Any] = {"headers": {"auth_key": self.key}}
        if body is not None:
            kw["headers"]["Content-Type"] = "application/json"
            kw["data"] = json.dumps(body)
        if not type(self.http).__module__.startswith(("httpx", "starlette")):
            kw["timeout"], kw["verify"] = self.timeout, self.verify
        else:
            kw["content"] = kw.pop("data", None)
        try:
            r = self.http.request(method, self.url + path, **kw)
        except Exception as e:  # noqa: BLE001
            raise TransientProviderError(f"rct-service {self.url}: {e}") from e
        if r.status_code == 401:
            raise ProviderError("rct-service: auth_key rejected")
        if r.status_code >= 400:
            raise ProviderError(f"rct-service {method} {path.split('/')[1]}: HTTP {r.status_code} {r.text[:300]}")
        return r

    @staticmethod
    def _p(vhd_path: str) -> str:
        return "/vdisk/" + quote(vhd_path, safe="")

    def info(self, vhd_path: str) -> dict[str, Any]:
        return self._req("GET", f"{self._p(vhd_path)}/info").json()

    def changes(self, vhd_path: str, rct_id: str) -> list[ByteRange]:
        rows = self._req("GET", f"{self._p(vhd_path)}/rct/{quote(rct_id, safe='')}/changes").json()
        return [ByteRange(int(r["offset"]), int(r["length"])) for r in rows]

    def read(self, vhd_path: str, offset: int, length: int) -> bytes:
        r = self._req("POST", f"{self._p(vhd_path)}/content", body=[{"offset": offset, "length": length}])
        if len(r.content) != length:
            raise TransientProviderError(f"rct-service: {len(r.content)} of {length} bytes at {offset}")
        return r.content


class RctServiceReader(DiskReader):
    def __init__(self, client: RctServiceClient, vhd_path: str):
        self.c, self.path = client, vhd_path
        info = client.info(vhd_path)
        if info.get("parent_path"):
            raise ProviderError(f"{vhd_path} is a differencing disk (parent {info['parent_path']})")
        self._size = int(info["virtual_size"])

    @property
    def size(self) -> int:
        return self._size

    def read(self, offset: int, length: int) -> bytes:
        length = max(0, min(length, self._size - offset))
        return self.c.read(self.path, offset, length) if length else b""
