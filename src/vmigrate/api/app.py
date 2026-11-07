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
from ..core.errors import ConfigError, MigrationError, ProviderNotImplemented
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

    @app.get("/secrets/status", tags=["meta"])
    def secret_status(_: Principal = Depends(admin)) -> dict[str, Any]:
        return svc.secrets.status()

    # ------------------------------------------------------------------ endpoints
    def ep_out(e: dict[str, Any]) -> m.EndpointOut:
        keys = sorted(svc.secrets.get(e["secret_id"]).keys()) if e.get("secret_id") else []
        return m.EndpointOut(id=e["id"], name=e["name"], type=e["type"], project=e["project"],
                             description=e["description"], config=e["config"], has_credentials=bool(keys),
                             credential_keys=keys, created_at=e["created_at"], updated_at=e["updated_at"])

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
        eid = repo.create_endpoint(p.project, body.name, body.type, body.config, sid, body.description)
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
        repo.delete_endpoint(eid)
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

    @app.get("/endpoints/{eid}/instances", tags=["endpoints"])
    async def list_instances(eid: str, p: Principal = Depends(viewer)) -> list[dict[str, Any]]:
        e = scoped(repo.get_endpoint(eid), p, "endpoint")

        def go() -> list[dict[str, Any]]:
            src = _source(e)
            try:
                return [{"id": v.id, "name": v.name} for v in src.list_vms()]
            finally:
                src.close()
        return await run_in_threadpool(go)

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
        return await run_in_threadpool(go)

    # ------------------------------------------------------------------ migrations
    def ex_out(x: dict[str, Any], counts: dict[str, dict[str, int]] | None = None) -> m.ExecutionOut:
        counts = counts if counts is not None else repo.task_counts([x["id"]])
        return m.ExecutionOut(**{k: x[k] for k in m.ExecutionOut.model_fields if k in x and k != "task_counts"},
                              task_counts=counts.get(x["id"], {}))

    def mig_out(r: dict[str, Any]) -> m.MigrationOut:
        st = r.get("state") or {}
        execs = repo.list_executions(r["id"])
        return m.MigrationOut(id=r["id"], name=r["name"], mode=r["mode"], project=r["project"],
                              source_endpoint_id=r["source_endpoint_id"], target_endpoint_id=r["target_endpoint_id"],
                              vm=r["vm"], options=r["options"], phase=st.get("phase"),
                              disks=len((st.get("spec") or {}).get("disks", [])) or None,
                              crosscheck=st.get("crosscheck"), created_at=r["created_at"], updated_at=r["updated_at"],
                              last_execution=ex_out(execs[0]) if execs else None)

    @app.get("/migrations", response_model=list[m.MigrationOut], tags=["migrations"])
    def list_migrations(p: Principal = Depends(viewer)) -> list[m.MigrationOut]:
        return [mig_out(r) for r in repo.list_migrations(None if p.role == "admin" else p.project, "migration")]

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
        svc.engine_for({"options": opts})  # validates options.engine before anything is stored
        mid = repo.create_migration(p.project, body.name, "migration", src["id"], tgt["id"], body.vm, opts, p.user_id)
        return mig_out(repo.get_migration(mid))  # type: ignore[arg-type]

    @app.get("/migrations/{mid}", response_model=m.MigrationOut, tags=["migrations"])
    def get_migration(mid: str, p: Principal = Depends(viewer)) -> m.MigrationOut:
        return mig_out(scoped(repo.get_migration(mid), p, "migration"))

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
        repo.delete_migration(mid)   # never touches source or target resources

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

    @app.post("/migrations/{mid}/cancel", tags=["migrations"])
    def cancel_migration(mid: str, p: Principal = Depends(operator)) -> dict[str, Any]:
        scoped(repo.get_migration(mid), p, "migration")
        x = repo.active_execution(mid)
        if x is None:
            raise HTTPException(409, "no active execution")
        repo.set_execution(x["id"], cancel_requested=True)
        return {"execution_id": x["id"], "cancel_requested": True}

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
    def exec_scoped(xid: str, p: Principal) -> dict[str, Any]:
        x = repo.get_execution(xid)
        if x is None:
            raise HTTPException(404, "execution not found")
        scoped(repo.get_migration(x["migration_id"]), p, "execution")
        return x

    @app.get("/executions", response_model=list[m.ExecutionOut], tags=["executions"])
    def list_execs(migration_id: str | None = None, status: str | None = None,
                   p: Principal = Depends(viewer)) -> list[m.ExecutionOut]:
        rows = repo.list_executions(migration_id, status)
        allowed = {x["id"] for x in repo.list_migrations(None if p.role == "admin" else p.project)}
        rows = [x for x in rows if x["migration_id"] in allowed]
        counts = repo.task_counts([x["id"] for x in rows])
        return [ex_out(x, counts) for x in rows]

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

    @app.post("/executions/{xid}/cancel", tags=["executions"])
    def cancel_exec(xid: str, p: Principal = Depends(operator)) -> dict[str, Any]:
        x = exec_scoped(xid, p)
        if ExecStatus(x["status"]) in TERMINAL_EXEC:
            raise HTTPException(409, f"execution is {x['status']}")
        repo.set_execution(xid, cancel_requested=True)
        return {"execution_id": xid, "cancel_requested": True}

    @app.post("/executions/{xid}/retry", response_model=m.ExecutionOut, tags=["executions"])
    def retry_exec(xid: str, p: Principal = Depends(operator)) -> m.ExecutionOut:
        from ..conductor.service import requeue_for_retry
        exec_scoped(xid, p)
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
        repo.delete_migration(rid)

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
                  "deploy_name": body.deploy_name or f"{(r['options'] or {}).get('target_name') or r['vm']}-dr"}
        return start_kind(r, "replica_deploy", body.dry_run, p, params)

    @app.post("/replicas/{rid}/delete-disks", response_model=m.ExecutionOut, status_code=202, tags=["replicas"])
    def delete_replica_disks(rid: str, body: m.StartRequest | None = None, p: Principal = Depends(operator)) -> m.ExecutionOut:
        return start_kind(rep_scoped(rid, p), "replica_delete", (body or m.StartRequest()).dry_run, p)

    @app.post("/replicas/{rid}/cancel", tags=["replicas"])
    def cancel_replica(rid: str, p: Principal = Depends(operator)) -> dict[str, Any]:
        rep_scoped(rid, p)
        x = repo.active_execution(rid)
        if x is None:
            raise HTTPException(409, "no active execution")
        repo.set_execution(x["id"], cancel_requested=True)
        return {"execution_id": x["id"], "cancel_requested": True}

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
            scoped(repo.get_migration(migration_id), p, "migration")
        allowed = None if p.role == "admin" else {x["id"] for x in repo.list_migrations(p.project)}
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
