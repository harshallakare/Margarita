"""Purpose: the REST API service (FastAPI). It never does heavy work: it validates input, writes
rows, and reads state. Executions are created PENDING and picked up by the conductor/worker.
The only provider calls it makes are short read-only ones the wizard needs (validate endpoint,
list VMs, inspect one VM with the disk-count gate), always with a dry-run RunContext.

Usage:
    uvicorn --factory vmigrate.api.app:create_app      (or: vmigrate-platform api)
    app = create_app(services)                          (tests)

Surface (all JSON, bearer auth; OpenAPI at /docs):
    POST /auth/token  GET /auth/me  POST /auth/logout        GET|POST /users (admin)
    GET  /providers                                           GET /health  GET /workers  GET /secrets/status
    /endpoints        CRUD + POST /{id}/validate, GET /{id}/instances, GET /{id}/instances/{vm}
    /migrations       CRUD + POST /{id}/start {dry_run: true by default}, POST /{id}/cancel, GET /{id}/report
    /executions       GET list, GET /{id}, /{id}/tasks, /{id}/logs, POST /{id}/cancel, /{id}/retry
    GET /events       Server-Sent Events (execution_id / migration_id filters, ?follow=false for a snapshot)
    /replicas         CRUD + POST /{id}/execute {dry_run}, /{id}/deploy {dry_run, restore_point, deploy_name},
                      /{id}/delete-disks {dry_run}, /{id}/cancel; GET|POST /{id}/schedules
    /schedules        GET list, GET /preview?cron=, PATCH|DELETE /{id}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.concurrency import run_in_threadpool

from ..config.loader import SECRET_KEYS
from ..core.context import RunContext
from ..core.cron import Cron, CronError
from ..core.errors import ConfigError, MigrationError, ProviderError, ProviderNotImplemented
from ..engine.dag import TERMINAL_EXEC, ExecStatus
from ..engine.report import render_markdown
from ..engine.safety import LEGACY_DISK_CAP, check_disk_counts
from ..engine.state import MigrationState
from ..platform.factory import Services
from ..platform.settings import Settings
from ..providers import FIELDS, SOURCES, TARGETS, build_source, build_target, support_matrix
from ..security.auth import Principal, hash_password, new_token, token_hash, verify_password
from . import schemas as m

bearer = HTTPBearer(auto_error=False)
VALIDATE_TIMEOUT = float(os.environ.get("VMIGRATE_VALIDATE_TIMEOUT", "60"))


def _secret_like(d: dict[str, Any], path: str = "config") -> list[str]:
    hits = []
    for k, v in d.items():
        if k.lower() in SECRET_KEYS:
            hits.append(f"{path}.{k}")
        if isinstance(v, dict):
            hits += _secret_like(v, f"{path}.{k}")
    return hits


def create_app(services: Services | None = None) -> FastAPI:
    svc = services or Services.from_settings(Settings.from_env())
    repo = svc.repo
    app = FastAPI(title="vmigrate", version="0.2.0",
                  description="Any-to-any VM migration + DR platform. Dry-run is the default everywhere.")
    if svc.settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=svc.settings.cors_origins, allow_methods=["*"],
                           allow_headers=["*"])
    app.state.services = svc

    # ------------------------------------------------------------------ audit trail (every change)
    from ..security import audit as aud
    alog = logging.getLogger("vmigrate.audit")

    def _actor(auth: str | None, cookie_token: str | None) -> dict[str, Any] | None:
        tok = auth.split(" ", 1)[1] if auth and auth.lower().startswith("bearer ") else cookie_token
        return repo.user_for_token(token_hash(tok)) if tok else None

    def _lookup_name(otype: str, oid: str) -> str | None:
        """Name of an existing record, so updates/starts/deletes read well in the trail."""
        try:
            if otype == "endpoint":
                r = repo.get_endpoint(oid)
            elif otype in ("migration", "replica"):
                r = repo.get_migration(oid, include_deleted=True)
            elif otype == "execution":
                x = repo.get_execution(oid)
                r = repo.get_migration(x["migration_id"], include_deleted=True) if x else None
            elif otype == "pool":
                r = repo.get_pool(oid)
            elif otype == "notification_channel":
                r = repo.get_channel(oid)
            elif otype == "alert":
                a = repo.get_alert(oid)
                return a["title"] if a else None
            elif otype == "schedule":
                sc = repo.get_schedule(oid)
                r = repo.get_migration(sc["migration_id"]) if sc else None
            else:
                return None
            return (r or {}).get("name")
        except Exception:  # noqa: BLE001 - best effort; deleted records have no name any more
            return None

    @app.middleware("http")
    async def audit_mw(request: Request, call_next: Any) -> Any:
        rid = uuid.uuid4().hex[:16]
        method = request.method.upper()
        watch = method in ("POST", "PUT", "PATCH", "DELETE")
        body_raw = await request.body() if watch else b""
        pre_name = None
        if method == "DELETE":                     # the record is gone afterwards: read its name first
            segs = [x for x in request.url.path.split("/") if x]
            if len(segs) >= 2 and segs[0] in aud.COLLECTIONS:
                pre_name = await run_in_threadpool(_lookup_name, aud.COLLECTIONS[segs[0]], segs[1])
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        route = getattr(request.scope.get("route"), "path", None)
        if not route or route.startswith("/ui") or (not watch and route not in aud.AUDITED_GETS):
            return response
        resp_body = b""
        if watch and response.status_code == 201 and "json" in response.headers.get("content-type", ""):
            chunks = [c async for c in response.body_iterator]    # created objects: keep id + name
            resp_body = b"".join(chunks)
            from starlette.responses import Response as _R
            response = _R(resp_body, status_code=201, headers=dict(response.headers), media_type="application/json")
        try:
            req = json.loads(body_raw) if body_raw else None
        except ValueError:
            req = None
        try:
            created = json.loads(resp_body) if resp_body else {}
        except ValueError:
            created = {}

        def write() -> None:
            u = _actor(request.headers.get("authorization"), request.query_params.get("access_token"))
            action, otype = aud.describe(method, route)
            params = dict(request.path_params)
            oid = (list(params.values())[-1] if params else None) or (created.get("id") if isinstance(created, dict) else None)
            name = None
            for src in (req, created):
                if isinstance(src, dict):
                    name = name or src.get("name") or src.get("username") or src.get("deploy_name")
            if not name and oid:
                name = pre_name or _lookup_name(otype, str(oid))
            actor = u["username"] if u else (req.get("username") if action == "auth.login" and isinstance(req, dict)
                                             else "anonymous")
            details: dict[str, Any] = {}
            if params:
                details["path_params"] = params
            if isinstance(req, (dict, list)):
                details["request"] = aud.redact(req)
            if request.query_params:
                details["query"] = aud.redact({k: v for k, v in request.query_params.items()})
            repo.add_audit({
                "actor": str(actor or "anonymous")[:128], "actor_id": u["id"] if u else None,
                "actor_role": u["role"] if u else None,
                "ip": request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                      or (request.client.host if request.client else None),
                "user_agent": (request.headers.get("user-agent") or "")[:256], "action": action,
                "object_type": otype, "object_id": str(oid)[:128] if oid else None,
                "object_name": str(name)[:256] if name else None, "project": u["project"] if u else None,
                "method": method, "path": request.url.path[:512], "status": response.status_code,
                "outcome": aud.outcome(response.status_code), "request_id": rid, "details": aud.clip(details)})
        try:
            await run_in_threadpool(write)
        except Exception:  # noqa: BLE001 - never fail the user's request because auditing failed; log it loudly
            alog.exception("could not write audit record for %s %s", method, request.url.path)
        return response

    # ------------------------------------------------------------------ errors
    @app.exception_handler(PermissionError)
    async def _perm(_: Request, e: PermissionError) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=403)

    @app.exception_handler(ConfigError)
    async def _cfg(_: Request, e: ConfigError) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=400)

    # ------------------------------------------------------------------ auth
    def principal(cred: HTTPAuthorizationCredentials | None = Depends(bearer),
                  access_token: str | None = Query(None, include_in_schema=False)) -> Principal:
        tok = cred.credentials if cred else access_token   # query token only for EventSource (SSE)
        if not tok:
            raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
        u = repo.user_for_token(token_hash(tok))
        if u is None:
            raise HTTPException(401, "invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
        return Principal(u["id"], u["username"], u["role"], u["project"])

    def need(role: str):
        def dep(p: Principal = Depends(principal)) -> Principal:
            p.require(role)
            return p
        return dep

    viewer, operator, admin = need("viewer"), need("operator"), need("admin")

    def scoped(row: dict[str, Any] | None, p: Principal, what: str) -> dict[str, Any]:
        if row is None or not p.sees_project(row["project"]):
            raise HTTPException(404, f"{what} not found")
        return row

    @app.post("/auth/token", response_model=m.TokenResponse, tags=["auth"])
    def login(body: m.TokenRequest) -> m.TokenResponse:
        u = repo.get_user_by_name(body.username)
        if u is None or u["disabled"] or not verify_password(body.password, u["password_hash"]):
            raise HTTPException(401, "bad username or password")
        tok, h = new_token()
        ttl = int(svc.settings.token_ttl_hours * 3600)
        repo.add_token(u["id"], h, ttl)
        return m.TokenResponse(token=tok, expires_in=ttl, username=u["username"], role=u["role"], project=u["project"])

    @app.get("/auth/me", tags=["auth"])
    def me(p: Principal = Depends(principal)) -> dict[str, Any]:
        return {"user_id": p.user_id, "username": p.username, "role": p.role, "project": p.project}

    @app.post("/auth/logout", tags=["auth"])
    def logout(cred: HTTPAuthorizationCredentials | None = Depends(bearer), _: Principal = Depends(principal)) -> dict:
        if cred:
            repo.revoke_token(token_hash(cred.credentials))
        return {"ok": True}

    @app.get("/users", tags=["auth"])
    def users(_: Principal = Depends(admin)) -> list[dict[str, Any]]:
        return [{k: v for k, v in u.items() if k != "password_hash"} for u in repo.list_users()]

    @app.post("/users", status_code=201, tags=["auth"])
    def create_user(body: m.UserCreate, _: Principal = Depends(admin)) -> dict[str, Any]:
        if repo.get_user_by_name(body.username):
            raise HTTPException(409, "user exists")
        return {"id": repo.create_user(body.username, hash_password(body.password), body.role, body.project)}

    # ------------------------------------------------------------------ meta
    @app.get("/health", tags=["meta"])
    def health() -> dict[str, Any]:
        return {"ok": True, "time": time.time()}

    @app.get("/providers", tags=["meta"])
    def providers(_: Principal = Depends(viewer)) -> dict[str, Any]:
        def rows(t: dict) -> list[dict[str, Any]]:
            return [{"type": k, "status": v[1], "notes": v[2], "fields": FIELDS.get(k, [])} for k, v in t.items()]
        return {"sources": rows(SOURCES), "targets": rows(TARGETS),
                "matrix": [dict(zip(("side", "type", "status", "notes"), r)) for r in support_matrix()]}

    @app.get("/workers", tags=["meta"])
    def workers(_: Principal = Depends(viewer)) -> list[dict[str, Any]]:
        now = time.time()
        return [{**w, "alive": now - w["heartbeat_at"] < 3 * svc.settings.lease_seconds} for w in repo.list_workers()]

    # ------------------------------------------------------------------ pools
    from ..platform import pools as _pools

    def pool_out(pl: dict[str, Any]) -> dict[str, Any]:
        mem = _pools.members(pl, repo.list_workers(), time.time() - 3 * svc.settings.lease_seconds)
        st = pl.get("state") or {}
        return {**{k: pl[k] for k in ("id", "name", "region", "description", "min_size", "max_size", "provision",
                                       "created_at", "updated_at")},
                "tag": _pools.tag(pl["name"]), "members": mem, "busy": sum(1 for x in mem if x["busy"]),
                "minions": st.get("minions", []), "last_error": st.get("last_error")}

    def get_pool_or_404(pid: str) -> dict[str, Any]:
        pl = repo.get_pool(pid)
        if pl is None:
            raise HTTPException(404, "pool not found")
        return pl

    # ------------------------------------------------------------------ alerts + notifications
    def alert_visible(a: dict[str, Any], p: Principal) -> bool:
        return a.get("project") is None or p.sees_project(a["project"])

    @app.get("/alerts", tags=["alerts"])
    def list_alerts(status: str | None = None, migration_id: str | None = None, limit: int = 200,
                    p: Principal = Depends(viewer)) -> list[dict[str, Any]]:
        if status not in (None, "firing", "resolved"):
            raise HTTPException(422, "status must be firing or resolved")
        rows = repo.list_alerts(status=status, migration_id=migration_id, limit=max(1, min(limit, 1000)))
        return [a for a in rows if alert_visible(a, p)]

    @app.get("/alerts/summary", tags=["alerts"])
    def alerts_summary(p: Principal = Depends(viewer)) -> dict[str, int]:
        rows = [a for a in repo.firing_alerts() if alert_visible(a, p)]
        return {"firing": len(rows), "critical": sum(a["severity"] == "critical" for a in rows),
                "unacknowledged": sum(not a.get("ack_at") for a in rows)}

    @app.post("/alerts/{aid}/ack", tags=["alerts"])
    def ack_alert(aid: str, p: Principal = Depends(operator)) -> dict[str, Any]:
        a = repo.get_alert(aid)
        if a is None or not alert_visible(a, p):
            raise HTTPException(404, "alert not found")
        if a["status"] != "firing":
            raise HTTPException(409, "alert is already resolved")
        repo.update_alert(aid, ack_by=p.username, ack_at=time.time())
        return repo.get_alert(aid)  # type: ignore[return-value]

    def channel_out(c: dict[str, Any]) -> dict[str, Any]:
        return {k: c.get(k) for k in ("id", "name", "type", "config", "enabled", "min_severity", "notify_resolved",
                                      "notify_success", "last_sent_at", "last_error", "created_at", "updated_at")} \
            | {"has_password": bool(c.get("secret_id"))}

    def channel_or_404(cid: str) -> dict[str, Any]:
        c = repo.get_channel(cid)
        if c is None:
            raise HTTPException(404, "notification channel not found")
        return c

    def build_channel(cfg: dict[str, Any], password: str | None) -> Any:
        from ..alerts.providers import problems
        bad = problems(cfg, password)
        if bad:
            raise HTTPException(422, "; ".join(bad))
        try:
            return svc.alert_manager().channel_factory(cfg, password)
        except ConfigError as e:
            raise HTTPException(422, str(e)) from e

    def check_channel(cfg: dict[str, Any], password: str | None, *, skip: bool = False) -> None:
        """Save-time check: SMTP connect + TLS + log in, or a Microsoft sign-in (no mail sent). 422 with
        the server's answer on failure. The settings are always validated, even with skip_check."""
        ch = build_channel(cfg, password)
        if skip:
            return
        try:
            ch.check()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(422, f"Connection check failed ({type(e).__name__}): {e}. Fix the settings, or "
                                     "save with skip_check to store them anyway.") from e

    @app.get("/notification-providers", tags=["alerts"])
    def notification_providers(_: Principal = Depends(admin)) -> list[dict[str, Any]]:
        """Email provider presets: pick one and enter only the account and its secret."""
        from ..alerts.providers import catalog
        return catalog()

    @app.post("/notification-channels/test", tags=["alerts"])
    def test_unsaved_channel(body: m.ChannelTest, _: Principal = Depends(admin)) -> dict[str, Any]:
        """Send a test email with the editor's current settings, before (or without) saving them."""
        pw = body.password
        if not pw and body.channel_id:
            c = channel_or_404(body.channel_id)
            pw = (svc.secrets.get(c["secret_id"]) or {}).get("password") if c.get("secret_id") else None
        cfg = body.config.model_dump()
        ch = build_channel(cfg, pw)
        try:
            svc.alert_manager().send_test(ch, "unsaved settings")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(422, f"test message failed ({type(e).__name__}): {e}") from e
        return {"ok": True, "sent_to": cfg.get("to")}

    @app.get("/notification-channels", tags=["alerts"])
    def list_channels(_: Principal = Depends(admin)) -> list[dict[str, Any]]:
        return [channel_out(c) for c in repo.list_channels()]

    @app.post("/notification-channels", status_code=201, tags=["alerts"])
    def create_channel(body: m.ChannelCreate, p: Principal = Depends(admin)) -> dict[str, Any]:
        if repo.get_channel_by_name(body.name):
            raise HTTPException(409, "a notification channel with this name exists")
        cfg = body.config.model_dump()
        check_channel(cfg, body.password, skip=body.skip_check)
        sid = svc.secrets.put({"password": body.password}) if body.password else None
        cid = repo.create_channel(name=body.name, type=body.type, config=cfg, secret_id=sid, enabled=body.enabled,
                                  min_severity=body.min_severity, notify_resolved=body.notify_resolved,
                                  notify_success=body.notify_success)
        return channel_out(channel_or_404(cid))

    @app.patch("/notification-channels/{cid}", tags=["alerts"])
    def update_channel(cid: str, body: m.ChannelUpdate, _: Principal = Depends(admin)) -> dict[str, Any]:
        c = channel_or_404(cid)
        vals = body.model_dump(exclude_unset=True, exclude={"password", "clear_password", "skip_check", "config"})
        cfg = body.config.model_dump() if body.config is not None else dict(c["config"])
        if body.config is not None:
            vals["config"] = cfg
        old_pw = (svc.secrets.get(c["secret_id"]) or {}).get("password") if c.get("secret_id") else None
        pw = None if body.clear_password else (body.password if body.password is not None else old_pw)
        if body.config is not None or body.password is not None or body.clear_password:
            check_channel(cfg, pw, skip=body.skip_check)
        if body.clear_password and c.get("secret_id"):
            svc.secrets.delete(c["secret_id"])
            vals["secret_id"] = None
        elif body.password is not None:
            if c.get("secret_id"):
                svc.secrets.replace(c["secret_id"], {"password": body.password})
            else:
                vals["secret_id"] = svc.secrets.put({"password": body.password})
        repo.update_channel(cid, **vals)
        return channel_out(channel_or_404(cid))

    @app.delete("/notification-channels/{cid}", status_code=204, tags=["alerts"])
    def delete_channel(cid: str, _: Principal = Depends(admin)) -> None:
        c = channel_or_404(cid)
        if c.get("secret_id"):
            svc.secrets.delete(c["secret_id"])
        repo.delete_channel(cid)

    @app.post("/notification-channels/{cid}/test", tags=["alerts"])
    def test_channel(cid: str, _: Principal = Depends(admin)) -> dict[str, Any]:
        c = channel_or_404(cid)
        try:
            svc.alert_manager().test_channel(c)
        except Exception as e:  # noqa: BLE001
            repo.update_channel(cid, last_error=f"{type(e).__name__}: {e}"[:1000])
            raise HTTPException(422, f"test message failed ({type(e).__name__}): {e}") from e
        return {"ok": True, "sent_to": c["config"].get("to")}

    @app.get("/pools", tags=["pools"])
    def list_pools(_: Principal = Depends(viewer)) -> list[dict[str, Any]]:
        return [pool_out(x) for x in repo.list_pools()]

    @app.post("/pools", status_code=201, tags=["pools"])
    def create_pool(body: m.PoolCreate, p: Principal = Depends(admin)) -> dict[str, Any]:
        if repo.get_pool_by_name(body.name):
            raise HTTPException(409, "a pool with this name exists")
        if body.max_size < body.min_size:
            raise HTTPException(422, "max_size must be >= min_size")
        if body.provision is not None and repo.get_endpoint(body.provision.endpoint_id) is None:
            raise HTTPException(422, "provision.endpoint_id not found")
        pid = repo.create_pool(body.name, region=body.region, description=body.description, min_size=body.min_size,
                               max_size=body.max_size, provision=body.provision.model_dump() if body.provision else None)
        repo.add_event("pool.created", {"name": body.name, "by": p.username})
        return pool_out(get_pool_or_404(pid))

    @app.get("/pools/{pid}", tags=["pools"])
    def get_pool(pid: str, _: Principal = Depends(viewer)) -> dict[str, Any]:
        return pool_out(get_pool_or_404(pid))

    @app.patch("/pools/{pid}", tags=["pools"])
    def update_pool(pid: str, body: m.PoolUpdate, _: Principal = Depends(admin)) -> dict[str, Any]:
        pl = get_pool_or_404(pid)
        vals = body.model_dump(exclude_unset=True)
        if "provision" in vals and vals["provision"] is not None:
            vals["provision"] = body.provision.model_dump()  # type: ignore[union-attr]
        lo, hi = vals.get("min_size", pl["min_size"]), vals.get("max_size", pl["max_size"])
        if hi < lo:
            raise HTTPException(422, "max_size must be >= min_size")
        repo.update_pool(pid, **vals)
        return pool_out(get_pool_or_404(pid))

    @app.delete("/pools/{pid}", status_code=204, tags=["pools"])
    def delete_pool(pid: str, _: Principal = Depends(admin)) -> None:
        pl = get_pool_or_404(pid)
        out = pool_out(pl)
        if out["members"] or any(x.get("status") in ("booting", "joined", "stuck") for x in out["minions"]):
            raise HTTPException(409, "pool still has live members or minions; stop/delete them first")
        repo.delete_pool(pid)

    @app.post("/pools/{pid}/enroll-token", tags=["pools"])
    def enroll_token(pid: str, body: m.EnrollTokenRequest | None = None, p: Principal = Depends(admin)) -> dict[str, Any]:
        pl = get_pool_or_404(pid)
        tok, exp = repo.create_enroll_token(pid, (body or m.EnrollTokenRequest()).ttl_minutes * 60, p.user_id)
        return {"token": tok, "expires_at": exp, "pool": pl["name"],
                "command": f"sudo bash deploy.sh --enroll <https-url-of-this-api> --enroll-token {tok}"}

    @app.delete("/pools/{pid}/minions/{server_id}", status_code=204, tags=["pools"])
    def delete_minion(pid: str, server_id: str, _: Principal = Depends(admin)) -> None:
        pl = get_pool_or_404(pid)
        st = dict(pl.get("state") or {})
        mins = list(st.get("minions") or [])
        hit = next((x for x in mins if x["server_id"] == server_id), None)
        if hit is None:
            raise HTTPException(404, "not a minion of this pool")
        svc.minion_provisioner(pl).delete(server_id)
        hit["status"] = "deleted"
        st["minions"] = mins
        repo.update_pool(pid, state=st)

    @app.post("/enroll", tags=["pools"])
    def enroll(body: m.EnrollRequest, request: Request) -> dict[str, Any]:
        """A new worker exchanges a single-use token for the shared DB URL + secret key."""
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        if proto != "https" and not svc.settings.allow_http_enroll:
            raise HTTPException(403, "enrollment sends the secret key: use HTTPS (or set VMIGRATE_ALLOW_HTTP_ENROLL=1 "
                                     "on a trusted lab network)")
        if not svc.settings.database_url.startswith("postgres"):
            raise HTTPException(409, "remote workers need a shared Postgres database; this server uses "
                                     f"{svc.settings.database_url.split(':', 1)[0]}")
        t = repo.consume_enroll_token(body.token, body.hostname or (request.client.host if request.client else "?"))
        if t is None:
            raise HTTPException(401, "invalid, used or expired enrollment token")
        pl = repo.get_pool(t["pool_id"]) if t.get("pool_id") else None
        repo.add_event("worker.enrolled", {"host": body.hostname, "pool": pl["name"] if pl else None})
        return {"database_url": svc.settings.database_url, "secret_key": svc.settings.secret_key,
                "region": pl.get("region") if pl else None, "tags": [_pools.tag(pl["name"])] if pl else []}

    @app.get("/secrets/status", tags=["meta"])
    def secret_status(_: Principal = Depends(admin)) -> dict[str, Any]:
        return svc.secrets.status()

    # ------------------------------------------------------------------ endpoints
    def ep_out(e: dict[str, Any]) -> m.EndpointOut:
        keys = sorted(svc.secrets.get(e["secret_id"]).keys()) if e.get("secret_id") else []
        return m.EndpointOut(id=e["id"], name=e["name"], type=e["type"], project=e["project"],
                             description=e["description"], config=e["config"], has_credentials=bool(keys),
                             credential_keys=keys, created_at=e["created_at"], updated_at=e["updated_at"],
                             region=e.get("region"), required_tags=list(e.get("required_tags") or []))

    def check_type(t: str) -> None:
        if t not in SOURCES and t not in TARGETS:
            raise HTTPException(400, f"unknown provider type {t!r}; known: {sorted(set(SOURCES) | set(TARGETS))}")
        if svc.settings.allowed_providers and t not in svc.settings.allowed_providers:
            raise HTTPException(400, f"provider type {t!r} is not enabled on this deployment")

    @app.get("/endpoints", response_model=list[m.EndpointOut], tags=["endpoints"])
    def list_endpoints(p: Principal = Depends(viewer)) -> list[m.EndpointOut]:
        return [ep_out(e) for e in repo.list_endpoints(None if p.role == "admin" else p.project)]

    def connection_problems(type_: str, name: str, config: dict[str, Any], creds: dict[str, Any]) -> list[str]:
        """Build the provider from the SUBMITTED settings (nothing stored yet) and run its
        connection check. Worker-host prerequisites are deliberately not checked here."""
        cfg = {**config, **creds, "type": type_}
        for table, build in ((SOURCES, build_source), (TARGETS, build_target)):
            if table.get(type_, (None,))[0] is None:
                continue
            try:
                prov = build(name, cfg, RunContext(dry_run=True))
            except Exception as ex:  # noqa: BLE001 - bad/missing settings
                return [f"{type(ex).__name__}: {ex}"]
            try:
                return prov.check_connection()
            except Exception as ex:  # noqa: BLE001
                return [f"{type(ex).__name__}: {ex}"]
            finally:
                try:
                    prov.close()
                except Exception:  # noqa: BLE001
                    pass
        return [f"provider {type_!r} is not implemented yet"]

    async def require_connection(type_: str, name: str, config: dict[str, Any], creds: dict[str, Any]) -> None:
        try:
            problems = await asyncio.wait_for(
                run_in_threadpool(connection_problems, type_, name, config, creds), timeout=VALIDATE_TIMEOUT)
        except TimeoutError:
            problems = [f"no answer within {VALIDATE_TIMEOUT:.0f}s (host unreachable or firewalled?)"]
        if problems:
            raise HTTPException(422, {"message": "connection check failed; endpoint not saved", "problems": problems})

    @app.post("/endpoints", response_model=m.EndpointOut, status_code=201, tags=["endpoints"])
    async def create_endpoint(body: m.EndpointCreate, p: Principal = Depends(operator)) -> m.EndpointOut:
        """Validates the connection with the submitted settings first; saves only if it works."""
        check_type(body.type)
        leaked = _secret_like(body.config)
        if leaked:
            raise HTTPException(400, f"secret-looking keys must go in 'credentials', not 'config': {leaked}")
        if any(e["name"] == body.name for e in repo.list_endpoints(p.project)):
            raise HTTPException(409, f"endpoint {body.name!r} exists")
        await require_connection(body.type, body.name, body.config, body.credentials)
        sid = svc.secrets.put(body.credentials) if body.credentials else None
        eid = repo.create_endpoint(p.project, body.name, body.type, body.config, sid, body.description,
                                   region=body.region or None, required_tags=body.required_tags)
        return ep_out(repo.get_endpoint(eid))  # type: ignore[arg-type]

    @app.get("/endpoints/{eid}", response_model=m.EndpointOut, tags=["endpoints"])
    def get_endpoint(eid: str, p: Principal = Depends(viewer)) -> m.EndpointOut:
        return ep_out(scoped(repo.get_endpoint(eid), p, "endpoint"))

    @app.patch("/endpoints/{eid}", response_model=m.EndpointOut, tags=["endpoints"])
    async def update_endpoint(eid: str, body: m.EndpointUpdate, p: Principal = Depends(operator)) -> m.EndpointOut:
        """Validates the connection with the MERGED settings first; saves only if it works."""
        e = scoped(repo.get_endpoint(eid), p, "endpoint")
        vals: dict[str, Any] = {}
        if body.description is not None:
            vals["description"] = body.description
        if body.region is not None:
            vals["region"] = body.region or None
        if body.required_tags is not None:
            vals["required_tags"] = [t.strip() for t in body.required_tags if t.strip()]
        if body.config is not None:
            leaked = _secret_like(body.config)
            if leaked:
                raise HTTPException(400, f"secret-looking keys must go in 'credentials': {leaked}")
            vals["config"] = body.config
        if body.config is not None or body.credentials is not None:
            creds = body.credentials if body.credentials is not None else (
                svc.secrets.get(e["secret_id"]) if e.get("secret_id") else {})
            await require_connection(e["type"], e["name"], body.config if body.config is not None else e["config"],
                                     creds)
        if body.credentials is not None:
            if e.get("secret_id"):
                svc.secrets.replace(e["secret_id"], body.credentials)
            else:
                vals["secret_id"] = svc.secrets.put(body.credentials)
        if vals:
            repo.update_endpoint(eid, **vals)
        return ep_out(repo.get_endpoint(eid))  # type: ignore[arg-type]

    @app.delete("/endpoints/{eid}", status_code=204, tags=["endpoints"])
    def delete_endpoint(eid: str, p: Principal = Depends(operator)) -> None:
        e = scoped(repo.get_endpoint(eid), p, "endpoint")
        if repo.endpoint_in_use(eid):
            raise HTTPException(409, "endpoint is used by a migration/replica; delete those first")
        repo.delete_endpoint(eid)   # also purges deleted migrations that used it, with their run logs
        if e.get("secret_id"):
            svc.secrets.delete(e["secret_id"])

    def _validate(e: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"endpoint": e["name"], "type": e["type"], "roles": {}}
        ctx = RunContext(dry_run=True)
        for role, table, build in (("source", SOURCES, svc.build_source), ("target", TARGETS, svc.build_target)):
            if e["type"] not in table or table[e["type"]][0] is None:
                continue
            try:
                prov = build(e, ctx)
                try:
                    problems = prov.check_connection() or prov.preflight(None)
                finally:
                    prov.close()
            except (MigrationError, Exception) as ex:  # noqa: BLE001 - report connection problems
                problems = [f"{type(ex).__name__}: {ex}"]
            out["roles"][role] = {"ok": not problems, "problems": problems}
        if not out["roles"]:
            out["roles"]["none"] = {"ok": False, "problems": [f"provider {e['type']!r} is not implemented yet"]}
        out["ok"] = all(r["ok"] for r in out["roles"].values())
        return out

    @app.post("/endpoints/{eid}/validate", tags=["endpoints"])
    async def validate_endpoint(eid: str, p: Principal = Depends(viewer)) -> dict[str, Any]:
        e = scoped(repo.get_endpoint(eid), p, "endpoint")
        return await run_in_threadpool(_validate, e)

    def _source(e: dict[str, Any]):
        try:
            return svc.build_source(e, RunContext(dry_run=True))
        except ProviderNotImplemented as ex:
            raise HTTPException(400, str(ex)) from ex

    def _resources(tgt: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"networks": [], "storage": [], "flavors": [], "images": [], "security_groups": [],
                               "errors": {}}
        try:
            for key, fn in (("networks", tgt.list_networks), ("storage", tgt.list_storage),
                            ("flavors", tgt.list_flavors), ("images", tgt.list_images),
                            ("security_groups", tgt.list_security_groups)):
                try:
                    out[key] = fn()
                except Exception as ex:  # noqa: BLE001 - report per list; the form falls back to text
                    out["errors"][key] = f"{type(ex).__name__}: {ex}"
        finally:
            tgt.close()
        return out

    @app.post("/endpoints/discover", tags=["endpoints"])
    async def discover_resources(body: m.EndpointDiscover, p: Principal = Depends(operator)) -> dict[str, Any]:
        """Images / networks / flavors / storage on a TARGET, from the settings typed in the endpoint
        form, so helper image/network and similar fields can be picked from a list. Nothing is saved."""
        check_type(body.type)
        if TARGETS.get(body.type, (None,))[0] is None:
            raise HTTPException(400, f"{body.type!r} is not an implemented target provider")
        creds: dict[str, Any] = {}
        if body.endpoint_id:
            e = scoped(repo.get_endpoint(body.endpoint_id), p, "endpoint")
            if e["type"] != body.type:
                raise HTTPException(400, "endpoint type mismatch")
            creds = svc.secrets.get(e["secret_id"]) if e.get("secret_id") else {}
        creds.update({k: v for k, v in body.credentials.items() if v not in (None, "")})
        cfg = {**body.config, **creds, "type": body.type}

        def go() -> dict[str, Any]:
            return _resources(build_target("discover", cfg, RunContext(dry_run=True)))
        try:
            return await asyncio.wait_for(provider_call(f"listing resources on {body.type}", go),
                                          timeout=VALIDATE_TIMEOUT)
        except TimeoutError as ex:
            raise HTTPException(504, f"no answer within {VALIDATE_TIMEOUT:.0f}s") from ex

    @app.get("/endpoints/{eid}/resources", tags=["endpoints"])
    async def target_resources(eid: str, p: Principal = Depends(viewer)) -> dict[str, Any]:
        """Networks / storage / flavors on a TARGET endpoint, for the wizard's mapping dropdowns.
        Each list is empty when the target does not enumerate it (the UI then offers free text)."""
        e = scoped(repo.get_endpoint(eid), p, "endpoint")
        if TARGETS.get(e["type"], (None,))[0] is None:
            raise HTTPException(400, f"{e['type']!r} is not an implemented target provider")

        return await run_in_threadpool(lambda: _resources(svc.build_target(e, RunContext(dry_run=True))))

    elog = logging.getLogger("vmigrate.api.endpoints")

    async def provider_call(what: str, fn: Any) -> Any:
        """Run a provider call in the threadpool; on failure log the full traceback on the server and
        return the real reason to the UI (instead of a bare "Internal Server Error")."""
        try:
            return await run_in_threadpool(fn)
        except HTTPException:
            raise
        except Exception as ex:  # noqa: BLE001 - every provider failure must reach the user with its reason
            elog.exception("%s failed", what)
            status = 422 if isinstance(ex, (ValueError, MigrationError)) and not isinstance(ex, ProviderError) else 502
            raise HTTPException(status, f"{what} failed: {type(ex).__name__}: {ex}") from ex

    @app.get("/endpoints/{eid}/instances", tags=["endpoints"])
    async def list_instances(eid: str, p: Principal = Depends(viewer)) -> list[dict[str, Any]]:
        e = scoped(repo.get_endpoint(eid), p, "endpoint")

        def go() -> list[dict[str, Any]]:
            src = _source(e)
            try:
                return [{"id": v.id, "name": v.name} for v in src.list_vms()]
            finally:
                src.close()
        return await provider_call(f"listing VMs on {e['name']}", go)

    @app.get("/endpoints/{eid}/instances/{vm}", tags=["endpoints"])
    async def inspect_instance(eid: str, vm: str, p: Principal = Depends(viewer)) -> dict[str, Any]:
        """Full IR of one VM plus the disk-count gate result, so the wizard shows the TRUE disk
        count up front (and refuses to continue when enumerations disagree)."""
        e = scoped(repo.get_endpoint(eid), p, "endpoint")

        def go() -> dict[str, Any]:
            src = _source(e)
            try:
                ref = src.find_vm(vm)
                spec = src.inspect_vm(ref)
                cross = src.disk_count_crosscheck(ref)
                gate: dict[str, Any] = {"ok": True, "counts": {"ir.inspect_vm": len(spec.disks), **cross}}
                try:
                    check_disk_counts(spec, cross)
                except MigrationError as ex:
                    gate.update(ok=False, error=str(ex))
                gate["legacy_cap_exceeded"] = len(spec.disks) >= LEGACY_DISK_CAP
                return {"vm": spec.to_dict(), "disk_gate": gate}
            finally:
                src.close()
        return await provider_call(f"inspecting {vm} on {e['name']}", go)

    # ------------------------------------------------------------------ migrations
    def ex_out(x: dict[str, Any], counts: dict[str, dict[str, int]] | None = None,
               transfers: dict[str, dict[str, Any]] | None = None) -> m.ExecutionOut:
        counts = counts if counts is not None else repo.task_counts([x["id"]])
        transfers = transfers if transfers is not None else repo.transfer_summary([x["id"]])
        return m.ExecutionOut(**{k: x[k] for k in m.ExecutionOut.model_fields
                                 if k in x and k not in ("task_counts", "transfer")},
                              task_counts=counts.get(x["id"], {}),
                              transfer=transfers.get(x["id"]))

    def mig_out(r: dict[str, Any]) -> m.MigrationOut:
        st = r.get("state") or {}
        execs = repo.list_executions(r["id"])
        return m.MigrationOut(id=r["id"], name=r["name"], mode=r["mode"], project=r["project"],
                              source_endpoint_id=r["source_endpoint_id"], target_endpoint_id=r["target_endpoint_id"],
                              vm=r["vm"], options=r["options"], phase=st.get("phase"),
                              disks=len((st.get("spec") or {}).get("disks", [])) or None,
                              crosscheck=st.get("crosscheck"), created_at=r["created_at"], updated_at=r["updated_at"],
                              last_execution=ex_out(execs[0]) if execs else None,
                              deleted_at=r.get("deleted_at"), deleted_by=r.get("deleted_by"))

    @app.get("/migrations", response_model=list[m.MigrationOut], tags=["migrations"])
    def list_migrations(deleted: bool = Query(False, description="true: deleted migrations AND replicas, whose "
                                                             "runs and logs are kept"),
                        p: Principal = Depends(viewer)) -> list[m.MigrationOut]:
        proj = None if p.role == "admin" else p.project
        if deleted:
            return [mig_out(r) for r in repo.list_migrations(proj, deleted=True)]
        return [mig_out(r) for r in repo.list_migrations(proj, "migration")]

    @app.post("/migrations", response_model=m.MigrationOut, status_code=201, tags=["migrations"])
    def create_migration(body: m.MigrationCreate, p: Principal = Depends(operator)) -> m.MigrationOut:
        src = scoped(repo.get_endpoint(body.source_endpoint_id), p, "source endpoint")
        tgt = scoped(repo.get_endpoint(body.target_endpoint_id), p, "target endpoint")
        if SOURCES.get(src["type"], (None,))[0] is None:
            raise HTTPException(400, f"{src['type']!r} is not an implemented source provider")
        if TARGETS.get(tgt["type"], (None,))[0] is None:
            raise HTTPException(400, f"{tgt['type']!r} is not an implemented target provider")
        if any(x["name"] == body.name for x in repo.list_migrations(p.project)):
            raise HTTPException(409, f"migration {body.name!r} exists")
        opts = body.options.model_dump(exclude_none=True)
        if opts.get("pool") and repo.get_pool_by_name(opts["pool"]) is None:
            raise HTTPException(422, f"worker pool {opts['pool']!r} does not exist")
        svc.engine_for({"options": opts})  # validates options.engine before anything is stored
        mid = repo.create_migration(p.project, body.name, "migration", src["id"], tgt["id"], body.vm, opts, p.user_id)
        return mig_out(repo.get_migration(mid))  # type: ignore[arg-type]

    @app.get("/migrations/{mid}", response_model=m.MigrationOut, tags=["migrations"])
    def get_migration(mid: str, include_deleted: bool = False, p: Principal = Depends(viewer)) -> m.MigrationOut:
        return mig_out(scoped(repo.get_migration(mid, include_deleted), p, "migration"))

    @app.patch("/migrations/{mid}", response_model=m.MigrationOut, tags=["migrations"])
    def update_migration(mid: str, body: m.MigrationUpdate, p: Principal = Depends(operator)) -> m.MigrationOut:
        scoped(repo.get_migration(mid), p, "migration")
        if repo.active_execution(mid):
            raise HTTPException(409, "cannot change options while an execution is active")
        svc.engine_for({"options": body.options.model_dump(exclude_none=True)})
        repo.update_migration(mid, options=body.options.model_dump(exclude_none=True))
        return mig_out(repo.get_migration(mid))  # type: ignore[arg-type]

    @app.delete("/migrations/{mid}", status_code=204, tags=["migrations"])
    def delete_migration(mid: str, p: Principal = Depends(operator)) -> None:
        if scoped(repo.get_migration(mid), p, "migration")["mode"] != "migration":
            raise HTTPException(409, "this is a replica; use DELETE /replicas/{id}")
        if repo.active_execution(mid):
            raise HTTPException(409, "migration has an active execution; cancel it first")
        repo.delete_migration(mid, p.username)   # hidden, runs + logs kept; never touches source or target

    @app.post("/migrations/{mid}/start", response_model=m.ExecutionOut, status_code=202, tags=["migrations"])
    def start_migration(mid: str, body: m.StartRequest | None = None, p: Principal = Depends(operator)) -> m.ExecutionOut:
        body = body or m.StartRequest()
        r = scoped(repo.get_migration(mid), p, "migration")
        if r["mode"] != "migration":
            raise HTTPException(409, "this is a replica; use /replicas/{id}/execute or /deploy")
        if (r.get("state") or {}).get("phase") == "done" and not body.dry_run:
            raise HTTPException(409, "migration already completed")
        try:
            xid = repo.create_execution(mid, kind="migration", dry_run=body.dry_run, user_id=p.user_id)
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e
        repo.add_event("execution.created", {"dry_run": body.dry_run, "by": p.username}, execution_id=xid,
                       migration_id=mid)
        return ex_out(repo.get_execution(xid))  # type: ignore[arg-type]

    def cancel_run(x: dict[str, Any], force: bool, p: Principal) -> dict[str, Any]:
        """Cancel asks the worker to stop: running steps end at their next check (about a second
        for waits and commands) and the failure cleanup is skipped. Force-cancel (like Coriolis's
        --force) also marks the run CANCELED at once, without waiting for the worker: use it when a
        cancel is stuck. The worker still stops in the background and removes its helper VM; a new
        run of the same migration is refused until it has (see Repo.stopping_execution)."""
        xid = x["id"]
        if not force:
            repo.set_execution(xid, cancel_requested=True)
            repo.add_event("execution.cancel_requested", {"by": p.username}, execution_id=xid,
                           migration_id=x["migration_id"])
            return {"execution_id": xid, "cancel_requested": True, "force": False}
        n = repo.force_cancel_execution(xid, p.username)
        if n < 0:
            raise HTTPException(409, f"execution is {(repo.get_execution(xid) or {}).get('status')}")
        repo.add_event("execution.canceled", {"force": True, "by": p.username, "tasks": n}, execution_id=xid,
                       migration_id=x["migration_id"])
        return {"execution_id": xid, "cancel_requested": True, "force": True, "status": "canceled"}

    @app.post("/migrations/{mid}/cancel", tags=["migrations"])
    def cancel_migration(mid: str, force: bool = False, p: Principal = Depends(operator)) -> dict[str, Any]:
        scoped(repo.get_migration(mid), p, "migration")
        x = repo.active_execution(mid)
        if x is None:
            raise HTTPException(409, "no active execution")
        return cancel_run(x, force, p)

    @app.get("/migrations/{mid}/report", tags=["migrations"])
    def report(mid: str, format: str = Query("json", pattern="^(json|md)$"), p: Principal = Depends(viewer)):
        r = scoped(repo.get_migration(mid), p, "migration")
        if not r.get("state") or r["mode"] != "migration":
            raise HTTPException(404, "no report yet (start a dry-run first)")
        st = MigrationState.from_dict(r["state"])
        if format == "md":
            return PlainTextResponse(render_markdown(st), media_type="text/markdown")
        spec = st.spec or {}
        disks = []
        for d in sorted(spec.get("disks", []), key=lambda d: d["order"]):
            ds = st.disks.get(d["id"])
            disks.append({"id": d["id"], "order": d["order"], "bus": d["bus"], "size_bytes": d["size_bytes"],
                          "copied": bool(ds and ds.copied), "committed": bool(ds and ds.committed),
                          "attached": bool(ds and ds.attached), "verify": ds.verify if ds else None})
        return {"migration": r["name"], "phase": st.phase.value, "crosscheck": st.crosscheck,
                "disk_count_match": bool(st.crosscheck) and len(set(st.crosscheck.values())) == 1,  # type: ignore[union-attr]
                "disks": disks, "morph": st.morph, "verification": st.verification, "errors": st.errors,
                "target_vm": st.target_vm}

    # ------------------------------------------------------------------ executions
    def exec_scoped(xid: str, p: Principal, write: bool = False) -> dict[str, Any]:
        """Runs of deleted migrations stay readable (logs, tasks, diagnostics) but cannot be acted on."""
        x = repo.get_execution(xid)
        if x is None:
            raise HTTPException(404, "execution not found")
        mg = scoped(repo.get_migration(x["migration_id"], include_deleted=True), p, "execution")
        if write and mg.get("deleted_at"):
            raise HTTPException(409, "its migration was deleted; the run is kept only for its logs")
        return x

    @app.get("/executions", response_model=list[m.ExecutionOut], tags=["executions"])
    def list_execs(migration_id: str | None = None, status: str | None = None,
                   p: Principal = Depends(viewer)) -> list[m.ExecutionOut]:
        rows = repo.list_executions(migration_id, status)
        allowed = {x["id"] for x in repo.list_migrations(None if p.role == "admin" else p.project, deleted=None)}
        rows = [x for x in rows if x["migration_id"] in allowed]
        ids = [x["id"] for x in rows]
        counts, transfers = repo.task_counts(ids), repo.transfer_summary(ids)
        return [ex_out(x, counts, transfers) for x in rows]

    @app.get("/executions/{xid}", response_model=m.ExecutionOut, tags=["executions"])
    def get_exec(xid: str, p: Principal = Depends(viewer)) -> m.ExecutionOut:
        return ex_out(exec_scoped(xid, p))

    @app.get("/executions/{xid}/tasks", response_model=list[m.TaskOut], tags=["executions"])
    def get_tasks(xid: str, p: Principal = Depends(viewer)) -> list[m.TaskOut]:
        exec_scoped(xid, p)
        return [m.TaskOut(**{k: t[k] for k in m.TaskOut.model_fields}) for t in repo.list_tasks(xid)]

    @app.get("/executions/{xid}/logs", tags=["executions"])
    def get_logs(xid: str, task_id: str | None = None, after: int = 0, limit: int = Query(1000, le=5000),
                 p: Principal = Depends(viewer)) -> list[dict[str, Any]]:
        exec_scoped(xid, p)
        return repo.get_logs(xid, task_id, after, limit)

    # ------------------------------------------------------------------ investigation: logs, events, bundle
    def visible_migrations(p: Principal) -> list[dict[str, Any]] | None:
        return None if p.role == "admin" else repo.list_migrations(p.project, deleted=None)

    @app.get("/logs", tags=["logs"])
    def search_logs(q: str | None = None, level: str | None = Query(None, description="comma list: ERROR,WARNING"),
                    migration_id: str | None = None, execution_id: str | None = None,
                    since: float | None = None, until: float | None = None, before_id: int | None = None,
                    limit: int = Query(300, le=5000), p: Principal = Depends(viewer)) -> list[dict[str, Any]]:
        """Run logs across executions, newest first, with the run and migration they belong to."""
        if execution_id:
            ids: list[str] | None = [exec_scoped(execution_id, p)["id"]]
        elif migration_id:
            scoped(repo.get_migration(migration_id, include_deleted=True), p, "migration")
            ids = [x["id"] for x in repo.list_executions(migration_id)]
        else:
            vis = visible_migrations(p)
            ids = None if vis is None else [x["id"] for mg in vis for x in repo.list_executions(mg["id"])]
        rows = repo.search_logs(execution_ids=ids, levels=[x for x in (level or "").split(",") if x], q=q,
                                since=since, until=until, before_id=before_id, limit=limit)
        ex_cache: dict[str, Any] = {}
        mig_cache: dict[str, Any] = {}
        for r in rows:
            x = ex_cache.get(r["execution_id"]) or ex_cache.setdefault(r["execution_id"], repo.get_execution(r["execution_id"]))
            if x:
                mg = mig_cache.get(x["migration_id"]) or mig_cache.setdefault(x["migration_id"], repo.get_migration(x["migration_id"], include_deleted=True))
                r["kind"], r["migration_id"] = x["kind"], x["migration_id"]
                r["migration_name"] = mg["name"] if mg else None
        return rows

    @app.get("/events/history", tags=["logs"])
    def event_history(type: str | None = Query(None, description="comma list of type prefixes, e.g. execution.,worker."),
                      migration_id: str | None = None, since: float | None = None, until: float | None = None,
                      before_id: int | None = None, limit: int = Query(300, le=5000),
                      p: Principal = Depends(viewer)) -> list[dict[str, Any]]:
        """Platform events (lifecycle, worker, schedule, requeue ...) newest first; non-streaming."""
        if migration_id:
            scoped(repo.get_migration(migration_id, include_deleted=True), p, "migration")
            mids: list[str] | None = [migration_id]
        else:
            vis = visible_migrations(p)
            mids = None if vis is None else [mg["id"] for mg in vis]
        return repo.search_events(migration_ids=mids, types=[x for x in (type or "").split(",") if x], since=since,
                                  until=until, before_id=before_id, limit=limit,
                                  include_global=p.role == "admin" or not migration_id)

    @app.get("/executions/{xid}/diagnostics", tags=["logs"])
    def diagnostics(xid: str, p: Principal = Depends(viewer)) -> Any:
        """One zip with everything needed to investigate a run. Secrets are never included."""
        import io
        import platform as _pf
        import zipfile

        from .. import __version__
        from ..security.audit import redact
        x = exec_scoped(xid, p)
        mg = repo.get_migration(x["migration_id"], include_deleted=True) or {}

        def ep(eid: str | None) -> dict[str, Any] | None:
            e = repo.get_endpoint(eid) if eid else None
            if not e:
                return None
            return {k: e.get(k) for k in ("id", "name", "type", "region", "required_tags")} | {
                "config": redact(e.get("config") or {}), "has_credentials": bool(e.get("secret_id"))}
        logs = repo.get_logs(xid, None, 0, 1_000_000)
        commit = None
        try:
            import subprocess as _sp
            commit = _sp.run(["git", "-C", os.path.dirname(__file__), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5).stdout.strip() or None
        except Exception:  # noqa: BLE001
            pass
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            def put(name: str, obj: Any) -> None:
                z.writestr(name, obj if isinstance(obj, str) else json.dumps(obj, indent=2, default=str))
            put("README.txt", f"Margarita diagnostics for execution {xid} ({x['kind']}, {x['status']}).\n"
                              "Secrets are not included: endpoint configs are redacted, credentials omitted.\n")
            put("execution.json", x)
            put("tasks.json", repo.list_tasks(xid))
            put("logs.txt", "\n".join(
                f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(r['ts']))}Z {r['level']:<7} "
                f"[{r.get('task_id') or '-'}] {r.get('logger') or ''} {r['message']}" for r in logs))
            put("events.json", repo.get_events(after=0, execution_id=xid))
            put("migration.json", {k: mg.get(k) for k in ("id", "name", "mode", "vm", "options", "created_at",
                                                          "updated_at", "project")})
            put("state.json", redact(mg.get("state") or {}))
            put("endpoints.json", {"source": ep(mg.get("source_endpoint_id")), "target": ep(mg.get("target_endpoint_id"))})
            put("workers.json", [{k: w.get(k) for k in ("id", "hostname", "region", "capabilities", "heartbeat_at",
                                                         "current_execution")} for w in repo.list_workers()])
            put("audit.json", repo.list_audit(object_id=x["migration_id"], limit=500)
                + repo.list_audit(object_id=xid, limit=500))
            put("versions.json", {"vmigrate": __version__, "commit": commit, "python": _pf.python_version(),
                                  "platform": _pf.platform(), "generated_at": time.time(),
                                  "generated_by": p.username})
            if mg.get("mode") == "migration" and mg.get("state"):
                try:
                    put("report.md", render_markdown(MigrationState.from_dict(mg["state"])))
                except Exception as e:  # noqa: BLE001
                    put("report-error.txt", str(e))
        name = f"margarita-diagnostics-{(mg.get('name') or 'run')}-{xid[:8]}.zip"
        return StreamingResponse(iter([buf.getvalue()]), media_type="application/zip",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ------------------------------------------------------------------ audit API (admin, read-only)
    @app.get("/audit", tags=["audit"])
    def audit_list(actor: str | None = None, action: str | None = None, object_type: str | None = None,
                   object_id: str | None = None, outcome: str | None = None, q: str | None = None,
                   since: float | None = None, until: float | None = None, before_id: int | None = None,
                   limit: int = Query(200, le=2000), _: Principal = Depends(admin)) -> list[dict[str, Any]]:
        rows = repo.list_audit(actor=actor, action=action, object_type=object_type, object_id=object_id,
                               outcome=outcome, q=q, since=since, until=until, before_id=before_id, limit=limit)
        return [{k: v for k, v in r.items() if k not in ("prev_hash",)} for r in rows]

    @app.get("/audit/verify", tags=["audit"])
    def audit_verify(_: Principal = Depends(admin)) -> dict[str, Any]:
        return repo.verify_audit()

    @app.get("/audit/export", tags=["audit"])
    def audit_export(since: float | None = None, until: float | None = None, _: Principal = Depends(admin)) -> Any:
        import csv
        import io
        rows = repo.list_audit(since=since, until=until, limit=10000)
        out = io.StringIO()
        cols = ["id", "ts", "time_utc", "actor", "actor_role", "ip", "action", "object_type", "object_id", "object_name",
                "project", "method", "path", "status", "outcome", "request_id", "details", "hash"]
        w = csv.DictWriter(out, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({**r, "time_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r["ts"])),
                        "details": json.dumps(r.get("details"), default=str)})
        return StreamingResponse(iter([out.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": 'attachment; filename="margarita-audit.csv"'})

    @app.post("/executions/{xid}/cancel", tags=["executions"])
    def cancel_exec(xid: str, force: bool = False, p: Principal = Depends(operator)) -> dict[str, Any]:
        x = exec_scoped(xid, p, write=True)
        if ExecStatus(x["status"]) in TERMINAL_EXEC:
            raise HTTPException(409, f"execution is {x['status']}")
        return cancel_run(x, force, p)

    @app.post("/executions/{xid}/retry", response_model=m.ExecutionOut, tags=["executions"])
    def retry_exec(xid: str, p: Principal = Depends(operator)) -> m.ExecutionOut:
        from ..conductor.service import requeue_for_retry
        exec_scoped(xid, p, write=True)
        try:
            requeue_for_retry(repo, xid)
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e
        return ex_out(repo.get_execution(xid))  # type: ignore[arg-type]

    # ------------------------------------------------------------------ replicas
    def cron_or_400(expr: str) -> Cron:
        try:
            return Cron.parse(expr)
        except CronError as e:
            raise HTTPException(400, f"invalid cron: {e}") from e

    def sched_out(sc: dict[str, Any]) -> m.ScheduleOut:
        up: list[float] = []
        if sc["enabled"]:
            try:
                up = Cron.parse(sc["cron"]).upcoming(time.time(), 3)
            except CronError:
                pass
        return m.ScheduleOut(id=sc["id"], replica_id=sc["migration_id"], cron=sc["cron"], enabled=sc["enabled"],
                             next_run_at=sc["next_run_at"], last_run_at=sc["last_run_at"],
                             last_execution_id=sc["last_execution_id"], last_skip_reason=sc["last_skip_reason"],
                             upcoming=up)

    def rep_scoped(rid: str, p: Principal) -> dict[str, Any]:
        r = scoped(repo.get_migration(rid), p, "replica")
        if r["mode"] != "replica":
            raise HTTPException(404, "replica not found")
        return r

    def rep_out(r: dict[str, Any]) -> m.ReplicaOut:
        st = r.get("state") or {}
        spec = st.get("spec") or {}
        execs = repo.list_executions(r["id"])
        cur = st.get("current")
        if cur:
            cur = {"id": cur["id"], "started": cur["started"], "last_error": cur.get("last_error"),
                   "disks_done": sum(1 for v in (cur.get("disks") or {}).values() if v.get("done"))}
        deps = [{k: v for k, v in d.items() if k != "state"} for d in st.get("deployments", [])]
        return m.ReplicaOut(
            id=r["id"], name=r["name"], project=r["project"], source_endpoint_id=r["source_endpoint_id"],
            target_endpoint_id=r["target_endpoint_id"], vm=r["vm"], options=r["options"],
            disks=len(spec.get("disks", [])) or None,
            disk_total_bytes=sum(d["size_bytes"] for d in spec.get("disks", [])) or None,
            crosscheck=st.get("crosscheck"), consistent=bool(st.get("consistent")), current_sync=cur,
            last_sync=(st.get("syncs") or [None])[-1], syncs=list(reversed(st.get("syncs", [])))[:50],
            restore_points=list(reversed(st.get("restore_points", []))), deployments=list(reversed(deps)),
            cutovers=list(reversed(st.get("cutovers", []))),
            helper=({k: st["helper"].get(k) for k in ("name", "host", "kept_at", "expires_at")}
                    | {"volumes": len(st["helper"].get("attached") or {})}) if st.get("helper") else None,
            errors=list(reversed(st.get("errors", [])))[:10],
            schedules=[sched_out(x) for x in repo.list_schedules(r["id"])],
            created_at=r["created_at"], updated_at=r["updated_at"], last_execution=ex_out(execs[0]) if execs else None)

    def start_kind(r: dict[str, Any], kind: str, dry_run: bool, p: Principal, params: dict[str, Any] | None = None):
        try:
            xid = repo.create_execution(r["id"], kind=kind, dry_run=dry_run, user_id=p.user_id, params=params)
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e
        repo.add_event("execution.created", {"dry_run": dry_run, "kind": kind, "by": p.username},
                       execution_id=xid, migration_id=r["id"])
        return ex_out(repo.get_execution(xid))  # type: ignore[arg-type]

    @app.get("/replicas", response_model=list[m.ReplicaOut], tags=["replicas"])
    def list_replicas(p: Principal = Depends(viewer)) -> list[m.ReplicaOut]:
        return [rep_out(r) for r in repo.list_migrations(None if p.role == "admin" else p.project, "replica")]

    @app.post("/replicas", response_model=m.ReplicaOut, status_code=201, tags=["replicas"])
    def create_replica(body: m.ReplicaCreate, p: Principal = Depends(operator)) -> m.ReplicaOut:
        src = scoped(repo.get_endpoint(body.source_endpoint_id), p, "source endpoint")
        tgt = scoped(repo.get_endpoint(body.target_endpoint_id), p, "target endpoint")
        if SOURCES.get(src["type"], (None,))[0] is None:
            raise HTTPException(400, f"{src['type']!r} is not an implemented source provider")
        if TARGETS.get(tgt["type"], (None,))[0] is None:
            raise HTTPException(400, f"{tgt['type']!r} is not an implemented target provider")
        if any(x["name"] == body.name for x in repo.list_migrations(p.project)):
            raise HTTPException(409, f"{body.name!r} exists")
        cron = cron_or_400(body.schedule) if body.schedule else None
        opts = body.options.model_dump(exclude_none=True)
        if opts.get("pool") and repo.get_pool_by_name(opts["pool"]) is None:
            raise HTTPException(422, f"worker pool {opts['pool']!r} does not exist")
        svc.engine_for({"options": opts})
        rid = repo.create_migration(p.project, body.name, "replica", src["id"], tgt["id"], body.vm, opts, p.user_id)
        if cron:
            repo.create_schedule(rid, cron.expr, True, cron.next_after(time.time()))
        return rep_out(repo.get_migration(rid))  # type: ignore[arg-type]

    @app.get("/replicas/{rid}", response_model=m.ReplicaOut, tags=["replicas"])
    def get_replica(rid: str, p: Principal = Depends(viewer)) -> m.ReplicaOut:
        return rep_out(rep_scoped(rid, p))

    @app.patch("/replicas/{rid}", response_model=m.ReplicaOut, tags=["replicas"])
    def update_replica(rid: str, body: m.MigrationUpdate, p: Principal = Depends(operator)) -> m.ReplicaOut:
        rep_scoped(rid, p)
        if repo.active_execution(rid):
            raise HTTPException(409, "cannot change options while an execution is active")
        opts = body.options.model_dump(exclude_none=True)
        if opts.get("pool") and repo.get_pool_by_name(opts["pool"]) is None:
            raise HTTPException(422, f"worker pool {opts['pool']!r} does not exist")
        svc.engine_for({"options": opts})
        repo.update_migration(rid, options=opts)
        return rep_out(repo.get_migration(rid))  # type: ignore[arg-type]

    @app.delete("/replicas/{rid}", status_code=204, tags=["replicas"])
    def delete_replica(rid: str, p: Principal = Depends(operator)) -> None:
        r = rep_scoped(rid, p)
        if repo.active_execution(rid):
            raise HTTPException(409, "replica has an active execution; cancel it first")
        if any(d.get("target") for d in ((r.get("state") or {}).get("disks") or {}).values()):
            raise HTTPException(409, "replica disks still exist on the target; run delete-disks first "
                                     "(or they would be orphaned)")
        repo.delete_migration(rid, p.username)

    @app.post("/replicas/{rid}/execute", response_model=m.ExecutionOut, status_code=202, tags=["replicas"])
    def execute_replica(rid: str, body: m.StartRequest | None = None, p: Principal = Depends(operator)) -> m.ExecutionOut:
        return start_kind(rep_scoped(rid, p), "replica_sync", (body or m.StartRequest()).dry_run, p)

    @app.post("/replicas/{rid}/deploy", response_model=m.ExecutionOut, status_code=202, tags=["replicas"])
    def deploy_replica(rid: str, body: m.DeployRequest, p: Principal = Depends(operator)) -> m.ExecutionOut:
        r = rep_scoped(rid, p)
        st = r.get("state") or {}
        if not st.get("syncs"):
            raise HTTPException(409, "the replica has no completed sync yet")
        if body.restore_point and not any(x["id"] == body.restore_point for x in st.get("restore_points", [])):
            raise HTTPException(404, "restore point not found")
        if not body.restore_point and not st.get("consistent"):
            raise HTTPException(409, "replica disks are mid-sync; finish a sync or choose a restore point")
        params = {"restore_point": body.restore_point, "deployment_id": uuid.uuid4().hex[:12],
                  "network_map": body.network_map, "preserve_mac": body.preserve_mac,
                  "deploy_name": body.deploy_name or f"{(r['options'] or {}).get('target_name') or r['vm']}-dr"}
        return start_kind(r, "replica_deploy", body.dry_run, p, params)

    @app.post("/replicas/{rid}/cutover", response_model=m.ExecutionOut, status_code=202, tags=["replicas"])
    def cutover_replica(rid: str, body: m.CutoverRequest, p: Principal = Depends(operator)) -> m.ExecutionOut:
        """Planned failover: [shut the source down if asked] -> final sync -> deploy. Shutting the source
        down is the only source state change Margarita makes, and only with shutdown_source=true."""
        r = rep_scoped(rid, p)
        if not (r.get("state") or {}).get("syncs"):
            raise HTTPException(409, "cutover needs a completed initial sync; run the replica first")
        params = {"shutdown_source": body.shutdown_source, "force_power_off": body.force_power_off,
                  "network_map": body.network_map, "preserve_mac": body.preserve_mac,
                  "shutdown_timeout": body.shutdown_timeout, "deployment_id": uuid.uuid4().hex[:12],
                  "cutover_id": uuid.uuid4().hex[:12],
                  "deploy_name": body.deploy_name or f"{(r['options'] or {}).get('target_name') or r['vm']}"}
        return start_kind(r, "replica_cutover", body.dry_run, p, params)

    @app.post("/replicas/{rid}/delete-disks", response_model=m.ExecutionOut, status_code=202, tags=["replicas"])
    def delete_replica_disks(rid: str, body: m.StartRequest | None = None, p: Principal = Depends(operator)) -> m.ExecutionOut:
        return start_kind(rep_scoped(rid, p), "replica_delete", (body or m.StartRequest()).dry_run, p)

    @app.post("/replicas/{rid}/cancel", tags=["replicas"])
    def cancel_replica(rid: str, force: bool = False, p: Principal = Depends(operator)) -> dict[str, Any]:
        rep_scoped(rid, p)
        x = repo.active_execution(rid)
        if x is None:
            raise HTTPException(409, "no active execution")
        return cancel_run(x, force, p)

    # ------------------------------------------------------------------ schedules
    @app.get("/schedules/preview", tags=["schedules"])
    def preview_cron(cron: str, _: Principal = Depends(viewer)) -> dict[str, Any]:
        try:
            return {"valid": True, "next": Cron.parse(cron).upcoming(time.time(), 5)}
        except CronError as e:
            return {"valid": False, "error": str(e), "next": []}

    @app.get("/schedules", response_model=list[m.ScheduleOut], tags=["schedules"])
    def list_schedules(p: Principal = Depends(viewer)) -> list[m.ScheduleOut]:
        allowed = {x["id"] for x in repo.list_migrations(None if p.role == "admin" else p.project, "replica")}
        return [sched_out(x) for x in repo.list_schedules() if x["migration_id"] in allowed]

    @app.get("/replicas/{rid}/schedules", response_model=list[m.ScheduleOut], tags=["schedules"])
    def replica_schedules(rid: str, p: Principal = Depends(viewer)) -> list[m.ScheduleOut]:
        rep_scoped(rid, p)
        return [sched_out(x) for x in repo.list_schedules(rid)]

    @app.post("/replicas/{rid}/schedules", response_model=m.ScheduleOut, status_code=201, tags=["schedules"])
    def add_schedule(rid: str, body: m.ScheduleCreate, p: Principal = Depends(operator)) -> m.ScheduleOut:
        rep_scoped(rid, p)
        c = cron_or_400(body.cron)
        sid = repo.create_schedule(rid, c.expr, body.enabled, c.next_after(time.time()))
        return sched_out(repo.get_schedule(sid))  # type: ignore[arg-type]

    def sched_scoped(sid: str, p: Principal) -> dict[str, Any]:
        sc = repo.get_schedule(sid)
        if sc is None:
            raise HTTPException(404, "schedule not found")
        rep_scoped(sc["migration_id"], p)
        return sc

    @app.patch("/schedules/{sid}", response_model=m.ScheduleOut, tags=["schedules"])
    def update_schedule(sid: str, body: m.ScheduleUpdate, p: Principal = Depends(operator)) -> m.ScheduleOut:
        sc = sched_scoped(sid, p)
        vals: dict[str, Any] = {}
        cron = sc["cron"]
        if body.cron is not None:
            cron = cron_or_400(body.cron).expr
            vals["cron"] = cron
        if body.enabled is not None:
            vals["enabled"] = body.enabled
        vals["next_run_at"] = Cron.parse(cron).next_after(time.time())  # re-arm from now
        repo.update_schedule(sid, **vals)
        return sched_out(repo.get_schedule(sid))  # type: ignore[arg-type]

    @app.delete("/schedules/{sid}", status_code=204, tags=["schedules"])
    def delete_schedule(sid: str, p: Principal = Depends(operator)) -> None:
        sched_scoped(sid, p)
        repo.delete_schedule(sid)

    # ------------------------------------------------------------------ events (SSE)
    @app.get("/events", tags=["events"])
    async def events(request: Request, execution_id: str | None = None, migration_id: str | None = None,
                     after: int = 0, follow: bool = True, last_event_id: str | None = Header(None),
                     p: Principal = Depends(viewer)) -> StreamingResponse:
        if execution_id:
            exec_scoped(execution_id, p)
        if migration_id:
            scoped(repo.get_migration(migration_id, include_deleted=True), p, "migration")
        allowed = None if p.role == "admin" else {x["id"] for x in repo.list_migrations(p.project, deleted=None)}
        cursor = int(last_event_id) if last_event_id and last_event_id.isdigit() else after

        async def gen() -> AsyncIterator[str]:
            nonlocal cursor
            idle = 0.0
            while True:
                rows = await run_in_threadpool(repo.get_events, after=cursor, execution_id=execution_id,
                                               migration_id=migration_id)
                for r in rows:
                    cursor = r["id"]
                    if allowed is not None and r["migration_id"] not in allowed:
                        continue
                    data = {"execution_id": r["execution_id"], "migration_id": r["migration_id"], "ts": r["ts"],
                            **(r["payload"] or {})}
                    yield f"id: {r['id']}\nevent: {r['type']}\ndata: {json.dumps(data, default=str)}\n\n"
                if not follow or await request.is_disconnected():
                    return
                if rows:
                    idle = 0.0
                else:
                    idle += 1.0
                    if idle >= 15:
                        idle = 0.0
                        yield ": keep-alive\n\n"
                await asyncio.sleep(1.0)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ web UI (static SPA build)
    web = svc.settings.web_dir
    if web and (web / "index.html").is_file():
        root = web.resolve()

        @app.get("/", include_in_schema=False)
        def _root() -> RedirectResponse:
            return RedirectResponse("/ui/")

        @app.get("/ui/{path:path}", include_in_schema=False)
        def _ui(path: str) -> FileResponse:
            f = (root / path).resolve()
            if path and f.is_file() and f.is_relative_to(root):
                return FileResponse(f)
            return FileResponse(root / "index.html")   # SPA fallback for client-side routes

    return app
