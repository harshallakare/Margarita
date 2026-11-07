"""Purpose: the export agent — runs on a worker at the SOURCE site and serves that site's source
providers (and their snapshot disks) to workers at other sites, for cross-site migrations and
replicas.

Usage:
    vmigrate-platform export-agent --port 8443 [--url https://this-host:8443] [--cert c --key k]
    app = make_agent_app(services, key)      # FastAPI app (tests use it with TestClient)

Protocol (all requests signed, see codec.py; HTTPS with a pinned self-signed certificate):
    POST /v1/call                 {endpoint_id, run_id, dry_run, method, args}  -> {result}
    POST /v1/open                 {endpoint_id, run_id, dry_run, snapshot, disk} -> {handle, size}
    GET  /v1/disks/{h}/read?offset=&length=   raw bytes; 204 + X-Zero when the range is all zeros;
                                              zlib body (X-Encoding: zlib) when the client asks
    GET  /v1/disks/{h}/allocated  -> {ranges: [[off, len], ...] | null}
    POST /v1/disks/{h}/close
    POST /v1/session/close        {endpoint_id, run_id, dry_run}
    GET  /v1/health

The agent builds the real provider with the run's dry_run flag, so every source-side safety rule
(require_execute, no live-disk reads, only own snapshots removed) is enforced here as well. Only
the SourceProvider methods in ALLOWED can be called. Providers are kept per (endpoint, run) so
state such as iSCSI sessions lives with the snapshot on this host; idle sessions expire.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
import zlib
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from ..core.context import RunContext
from ..core.interfaces import DiskReader, SourceProvider
from .codec import decode, encode, verify

log = logging.getLogger("vmigrate.export")

ALLOWED = {"list_vms", "find_vm", "inspect_vm", "disk_count_crosscheck", "create_snapshot", "snapshot_disk_count",
           "supports_cbt", "checkpoint", "keep_for_cbt", "retire_snapshot", "release_checkpoint", "changed_regions",
           "cleanup", "preflight", "check_connection", "shutdown_vm"}
MAX_READ = 64 << 20
IDLE_S = 4 * 3600


@dataclass
class _Session:
    provider: SourceProvider
    used: float = field(default_factory=time.time)
    handles: set[str] = field(default_factory=set)


class ExportAgent:
    def __init__(self, services: Any, key: bytes):
        self.svc, self.key = services, key
        self._lock = threading.RLock()
        self._sessions: dict[tuple[str, str, bool], _Session] = {}
        self._readers: dict[str, tuple[tuple[str, str, bool], DiskReader]] = {}

    def session(self, endpoint_id: str, run_id: str, dry_run: bool) -> _Session:
        k = (endpoint_id, run_id, bool(dry_run))
        with self._lock:
            s = self._sessions.get(k)
            if s is None:
                ep = self.svc.repo.get_endpoint(endpoint_id)
                if ep is None:
                    raise LookupError(f"endpoint {endpoint_id} not found")
                ctx = RunContext(dry_run=bool(dry_run), run_id=run_id,
                                 logger=logging.getLogger(f"vmigrate.export.{run_id[:8]}"))
                s = self._sessions[k] = _Session(self.svc.build_source(ep, ctx))
            s.used = time.time()
            return s

    def close_session(self, k: tuple[str, str, bool]) -> None:
        with self._lock:
            s = self._sessions.pop(k, None)
            if s is None:
                return
            for h in list(s.handles):
                self.close_reader(h)
        try:
            s.provider.close()
        except Exception:  # noqa: BLE001
            log.exception("closing provider for %s", k)

    def close_reader(self, h: str) -> None:
        with self._lock:
            item = self._readers.pop(h, None)
            if item:
                s = self._sessions.get(item[0])
                if s:
                    s.handles.discard(h)
        if item:
            try:
                item[1].close()
            except Exception:  # noqa: BLE001
                log.exception("closing reader %s", h)

    def reader(self, h: str) -> DiskReader:
        with self._lock:
            item = self._readers.get(h)
            if item is None:
                raise LookupError(f"unknown disk handle {h}")
            s = self._sessions.get(item[0])
            if s:
                s.used = time.time()
            return item[1]

    def open(self, k: tuple[str, str, bool], snapshot: Any, disk: Any) -> tuple[str, int]:
        s = self.session(*k)
        r = s.provider.open_disk(snapshot, disk)
        h = uuid.uuid4().hex
        with self._lock:
            self._readers[h] = (k, r)
            s.handles.add(h)
        return h, r.size

    def expire(self, now: float | None = None) -> int:
        now = now or time.time()
        with self._lock:
            old = [k for k, s in self._sessions.items() if now - s.used > IDLE_S]
        for k in old:
            self.close_session(k)
        return len(old)


def make_agent_app(services: Any, key: bytes, agent: ExportAgent | None = None) -> Any:
    ag = agent or ExportAgent(services, key)
    app = FastAPI(title="Margarita export agent", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.agent = ag

    @app.middleware("http")
    async def auth(request: Request, call_next: Any) -> Any:
        if request.url.path != "/v1/health":
            body = await request.body()
            path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            if not verify(key, request.method, path, body, request.headers.get("x-vmigrate-auth")):
                return JSONResponse({"error": {"type": "AuthError", "message": "bad or expired signature"}},
                                    status_code=401)
        return await call_next(request)

    def err(e: Exception) -> JSONResponse:
        code = 404 if isinstance(e, LookupError) else 400 if isinstance(e, (TypeError, ValueError)) else 500
        return JSONResponse({"error": {"type": type(e).__name__, "message": str(e)}}, status_code=code)

    def key_of(b: dict[str, Any]) -> tuple[str, str, bool]:
        return (str(b["endpoint_id"]), str(b["run_id"]), bool(b.get("dry_run", True)))

    @app.get("/v1/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "sessions": len(ag._sessions), "readers": len(ag._readers)}

    @app.post("/v1/call")
    async def call(request: Request) -> Any:
        b = await request.json()
        method = b.get("method")
        if method not in ALLOWED:
            return err(PermissionError(f"method {method!r} is not exported"))

        def go() -> Any:
            s = ag.session(*key_of(b))
            args = decode(b.get("args") or [])
            kwargs = decode(b.get("kwargs") or {})
            return encode(getattr(s.provider, method)(*args, **kwargs))
        try:
            return {"result": await run_in_threadpool(go)}
        except Exception as e:  # noqa: BLE001 - shipped back to the caller with its type
            return err(e)

    @app.post("/v1/open")
    async def open_disk(request: Request) -> Any:
        b = await request.json()
        try:
            h, size = await run_in_threadpool(ag.open, key_of(b), decode(b["snapshot"]), decode(b["disk"]))
            return {"handle": h, "size": size}
        except Exception as e:  # noqa: BLE001
            return err(e)

    @app.get("/v1/disks/{h}/read")
    async def read(h: str, offset: int, length: int, request: Request) -> Any:
        if offset < 0 or length < 0 or length > MAX_READ:
            raise HTTPException(400, f"length must be 0..{MAX_READ}")
        try:
            r = ag.reader(h)
            buf = await run_in_threadpool(r.read, offset, length)
        except Exception as e:  # noqa: BLE001
            return err(e)
        if buf and buf.count(0) == len(buf):
            return Response(status_code=204, headers={"X-Zero": str(len(buf))})
        if request.headers.get("x-compress") == "zlib":
            z = zlib.compress(buf, 1)
            if len(z) < len(buf) * 0.9:
                return Response(z, media_type="application/octet-stream",
                                headers={"X-Encoding": "zlib", "X-Length": str(len(buf))})
        return Response(buf, media_type="application/octet-stream", headers={"X-Length": str(len(buf))})

    @app.get("/v1/disks/{h}/allocated")
    async def allocated(h: str) -> Any:
        try:
            rs = await run_in_threadpool(ag.reader(h).allocated_ranges)
        except Exception as e:  # noqa: BLE001
            return err(e)
        return {"ranges": None if rs is None else [[x.offset, x.length] for x in rs]}

    @app.post("/v1/disks/{h}/close")
    async def close_disk(h: str) -> dict[str, bool]:
        await run_in_threadpool(ag.close_reader, h)
        return {"closed": True}

    @app.post("/v1/session/close")
    async def close_session(request: Request) -> dict[str, bool]:
        b = await request.json()
        await run_in_threadpool(ag.close_session, key_of(b))
        return {"closed": True}

    return app


# --------------------------------------------------------------------------- TLS + registration
def ensure_self_signed(cert_path: Any, key_path: Any, hostname: str) -> None:
    """Create a self-signed certificate once; peers pin its SHA-256 fingerprint (published in the
    workers table), so no CA is needed."""
    from pathlib import Path
    cert_path, key_path = Path(cert_path), Path(key_path)
    if cert_path.exists() and key_path.exists():
        return
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    k = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
            .sign(k, hashes.SHA256()))
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    key_path.chmod(0o600)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def cert_fingerprint(cert_path: Any) -> str:
    from pathlib import Path

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    c = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    return c.fingerprint(hashes.SHA256()).hex()


def heartbeat_loop(services: Any, agent: ExportAgent, *, agent_id: str, url: str, fingerprint: str | None,
                   region: str | None, stop: threading.Event, every: float = 10.0) -> None:
    import os
    import socket

    from ..core.routing import detect_capabilities
    caps = sorted(detect_capabilities() | {"export-agent"})
    while not stop.is_set():
        try:
            services.repo.worker_heartbeat(agent_id, socket.gethostname(), os.getpid(), None, region=region,
                                           capabilities=caps, export_url=url, export_fp=fingerprint)
            agent.expire()
        except Exception:  # noqa: BLE001
            log.exception("export agent heartbeat failed")
        stop.wait(every)
