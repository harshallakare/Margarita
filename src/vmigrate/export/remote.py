"""Purpose: RemoteSource — a SourceProvider that forwards every call to an export agent at the
source's site, and RemoteDiskReader, which reads snapshot disks from it over HTTPS.

Usage (normally done by platform.factory for cross-site work):
    src = RemoteSource(name, {"type": "nutanix"}, ctx, endpoint_id=ep["id"], key=derive_key(secret),
                       locate=lambda: ("https://agent:8443", "ab12..fingerprint"))
The agent is located lazily on first use, so a replica deploy (target-only) never needs the
source site to be reachable.
"""

from __future__ import annotations

import json
import time
import zlib
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode

from ..core import errors as E
from ..core.context import RunContext
from ..core.interfaces import CheckpointRef, DiskReader, SnapshotRef, SourceProvider, VmRef
from ..core.ranges import ByteRange
from ..ir import DiskSpec, VmSpec
from .codec import decode, encode, sign

_ERRORS: dict[str, type[Exception]] = {
    n: getattr(E, n) for n in dir(E) if isinstance(getattr(E, n), type) and issubclass(getattr(E, n), Exception)}
_ERRORS["NotImplementedError"] = NotImplementedError


def _session(fingerprint: str | None) -> Any:
    import requests
    from requests.adapters import HTTPAdapter

    class Pinned(HTTPAdapter):
        def init_poolmanager(self, *a: Any, **kw: Any) -> None:
            kw["assert_fingerprint"] = fingerprint
            kw["cert_reqs"] = "CERT_NONE"
            super().init_poolmanager(*a, **kw)

    s = requests.Session()
    if fingerprint:
        s.mount("https://", Pinned())
    return s


class AgentClient:
    def __init__(self, url: str, key: bytes, *, fingerprint: str | None = None, http: Any = None,
                 timeout: float = 600, retries: int = 3):
        self.url = url.rstrip("/")
        self.key, self.timeout, self.retries = key, timeout, retries
        if self.url.startswith("http://") and http is None:
            raise E.ConfigError(f"export agent {url}: plain http is refused (disk data would cross sites "
                                "unencrypted); run the agent with TLS")
        self.http = http if http is not None else _session(fingerprint)
        self.verify = False if fingerprint else True

    def request(self, method: str, path: str, *, body: Any = None, query: dict[str, Any] | None = None,
                headers: dict[str, str] | None = None) -> Any:
        if query:
            path = f"{path}?{urlencode(query)}"
        data = json.dumps(body).encode() if body is not None else b""
        last: Exception | None = None
        for attempt in range(self.retries):
            h = {"X-Vmigrate-Auth": sign(self.key, method, path, data), **(headers or {})}
            if body is not None:
                h["Content-Type"] = "application/json"
            try:
                kw: dict[str, Any] = {"headers": h, "content" if _is_httpx(self.http) else "data": data or None}
                if not _is_httpx(self.http):
                    kw["timeout"], kw["verify"] = self.timeout, self.verify
                r = self.http.request(method, self.url + path, **kw)
            except Exception as e:  # noqa: BLE001 - network: retry, then transient
                last = e
                time.sleep(min(2 ** attempt, 10))
                continue
            if r.status_code >= 400:
                try:
                    er = r.json().get("error") or {}
                except ValueError:
                    er = {"type": "ProviderError", "message": r.text[:500]}
                cls = _ERRORS.get(er.get("type", ""), E.ProviderError)
                raise cls(f"[export agent] {er.get('message', r.status_code)}")
            return r
        raise E.TransientProviderError(f"export agent {self.url}{path}: {last}")


def _is_httpx(http: Any) -> bool:
    return type(http).__module__.startswith(("httpx", "starlette", "fastapi"))


class RemoteDiskReader(DiskReader):
    def __init__(self, client: AgentClient, handle: str, size: int):
        self.c, self.h, self._size = client, handle, size

    @property
    def size(self) -> int:
        return self._size

    def read(self, offset: int, length: int) -> bytes:
        length = max(0, min(length, self._size - offset))
        if length == 0:
            return b""
        r = self.c.request("GET", f"/v1/disks/{self.h}/read", query={"offset": offset, "length": length},
                           headers={"X-Compress": "zlib"})
        if r.status_code == 204:
            return bytes(int(r.headers.get("X-Zero", length)))
        buf = r.content
        if r.headers.get("X-Encoding") == "zlib":
            buf = zlib.decompress(buf)
        if len(buf) != int(r.headers.get("X-Length", len(buf))):
            raise E.TransientProviderError(f"export agent: truncated read at {offset}")
        return buf

    def allocated_ranges(self) -> list[ByteRange] | None:
        rs = self.c.request("GET", f"/v1/disks/{self.h}/allocated").json()["ranges"]
        return None if rs is None else [ByteRange(o, n) for o, n in rs]

    def close(self) -> None:
        try:
            self.c.request("POST", f"/v1/disks/{self.h}/close", body={})
        except Exception:  # noqa: BLE001 - best effort; the agent expires idle handles
            pass


class RemoteSource(SourceProvider):
    """Every SourceProvider call runs on the export agent next to the real platform."""

    def __init__(self, name: str, config: Mapping[str, Any], ctx: RunContext, *, endpoint_id: str, key: bytes,
                 locate: Callable[[], tuple[str, str | None]], http: Any = None):
        super().__init__(name, config, ctx)
        self.type_name = str(config.get("type", "remote"))
        self.endpoint_id, self.key, self._locate, self._http = endpoint_id, key, locate, http
        self._client: AgentClient | None = None
        self.agent_url: str | None = None

    @property
    def client(self) -> AgentClient:
        if self._client is None:
            url, fp = self._locate()
            self.agent_url = url
            self._client = AgentClient(url, self.key, fingerprint=fp, http=self._http)
        return self._client

    def _sess(self) -> dict[str, Any]:
        return {"endpoint_id": self.endpoint_id, "run_id": self.ctx.run_id, "dry_run": self.ctx.dry_run}

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        r = self.client.request("POST", "/v1/call", body={**self._sess(), "method": method,
                                                           "args": encode(list(args)), "kwargs": encode(kwargs)})
        return decode(r.json()["result"])

    # ---- contract ----------------------------------------------------------------
    def preflight(self, spec: VmSpec | None = None) -> list[str]:
        return list(self._call("preflight", spec))

    def check_connection(self) -> list[str]:
        return list(self._call("check_connection"))

    def list_vms(self) -> list[VmRef]:
        return self._call("list_vms")

    def find_vm(self, name_or_id: str) -> VmRef:
        return self._call("find_vm", name_or_id)

    def inspect_vm(self, vm: VmRef) -> VmSpec:
        return self._call("inspect_vm", vm)

    def disk_count_crosscheck(self, vm: VmRef) -> dict[str, int]:
        return self._call("disk_count_crosscheck", vm)

    def create_snapshot(self, vm: VmRef, app_consistent: bool) -> SnapshotRef:
        self.ctx.require_execute("remote snapshot", vm=vm.name, agent=self.agent_url or "?")
        return self._call("create_snapshot", vm, app_consistent)

    def open_disk(self, snapshot: SnapshotRef, disk: DiskSpec) -> DiskReader:
        r = self.client.request("POST", "/v1/open", body={**self._sess(), "snapshot": encode(snapshot),
                                                           "disk": encode(disk)}).json()
        return RemoteDiskReader(self.client, r["handle"], int(r["size"]))

    def snapshot_disk_count(self, snapshot: SnapshotRef) -> int | None:
        return self._call("snapshot_disk_count", snapshot)

    def supports_cbt(self) -> bool:
        return bool(self._call("supports_cbt"))

    def checkpoint(self, snapshot: SnapshotRef) -> CheckpointRef:
        return self._call("checkpoint", snapshot)

    def keep_for_cbt(self) -> bool:
        return bool(self._call("keep_for_cbt"))

    def retire_snapshot(self, snapshot: SnapshotRef, previous: CheckpointRef | None) -> CheckpointRef:
        return self._call("retire_snapshot", snapshot, previous)

    def release_checkpoint(self, checkpoint: CheckpointRef) -> None:
        self._call("release_checkpoint", checkpoint)

    def changed_regions(self, disk: DiskSpec, base: CheckpointRef, cur: CheckpointRef) -> list[ByteRange]:
        return self._call("changed_regions", disk, base, cur)

    def cleanup(self, snapshot: SnapshotRef) -> None:
        self._call("cleanup", snapshot)

    def shutdown_vm(self, vm: VmRef, *, force: bool = False, timeout: int = 900) -> str:
        return self._call("shutdown_vm", vm, force=force, timeout=timeout)

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.request("POST", "/v1/session/close", body=self._sess())
            except Exception:  # noqa: BLE001
                pass
