"""Purpose: data access for the platform: endpoints, migrations, executions, tasks, logs, events,
users/tokens, workers. It also provides the DB-backed adapters the engine plugs into:
  * SqlTaskStore   — engine.dag.TaskStore for one execution (tasks table)
  * DbStateStore   — engine.state.StateStore-compatible, persists MigrationState in migrations.state
  * the work queue — executions.status + lease (claim / renew / release / reclaim_expired)

Usage:
    repo = Repo(make_engine(url)); repo.init_schema()
    eid = repo.create_execution(mig_id, kind="migration", dry_run=True, user_id=...)
    exec_id = repo.claim_execution("worker-1", lease_s=60)       # worker
    SqlTaskStore(repo, exec_id); DbStateStore(repo, mig_id)

Queue note: the queue is the executions table (atomic conditional UPDATE, safe on Postgres and
SQLite). RabbitMQ/Redis can replace claim/renew later without touching the conductor or worker.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.engine import Engine

from ..engine.dag import ExecStatus, TaskRecord, TaskStatus
from ..engine.state import MigrationState
from . import schema as s

ACTIVE_EXEC = (ExecStatus.PENDING.value, ExecStatus.QUEUED.value, ExecStatus.RUNNING.value)


def _id() -> str:
    return uuid.uuid4().hex


def _row(r: Any) -> dict[str, Any] | None:
    return dict(r._mapping) if r is not None else None


class Repo:
    def __init__(self, engine: Engine):
        self.engine = engine

    def init_schema(self) -> None:
        s.upgrade_schema(self.engine)

    # ------------------------------------------------------------------ users / tokens
    def create_user(self, username: str, password_hash: str, role: str, project: str = "default") -> str:
        uid = _id()
        with self.engine.begin() as c:
            c.execute(insert(s.users).values(id=uid, username=username, password_hash=password_hash, role=role,
                                             project=project, disabled=False, created_at=time.time()))
        return uid

    def get_user_by_name(self, username: str) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            return _row(c.execute(select(s.users).where(s.users.c.username == username)).first())

    def list_users(self) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            return [_row(r) for r in c.execute(select(s.users).order_by(s.users.c.username))]  # type: ignore[misc]

    def count_users(self) -> int:
        with self.engine.connect() as c:
            return int(c.execute(select(func.count()).select_from(s.users)).scalar_one())

    def add_token(self, user_id: str, token_hash: str, ttl_s: float) -> None:
        now = time.time()
        with self.engine.begin() as c:
            c.execute(insert(s.api_tokens).values(id=_id(), user_id=user_id, token_hash=token_hash,
                                                  created_at=now, expires_at=now + ttl_s))

    def user_for_token(self, token_hash: str) -> dict[str, Any] | None:
        q = (select(s.users).join(s.api_tokens, s.api_tokens.c.user_id == s.users.c.id)
             .where(s.api_tokens.c.token_hash == token_hash, s.api_tokens.c.expires_at > time.time(),
                    s.users.c.disabled.is_(False)))
        with self.engine.connect() as c:
            return _row(c.execute(q).first())

    def revoke_token(self, token_hash: str) -> None:
        with self.engine.begin() as c:
            c.execute(delete(s.api_tokens).where(s.api_tokens.c.token_hash == token_hash))

    # ------------------------------------------------------------------ endpoints
    def create_endpoint(self, project: str, name: str, type_: str, config: dict[str, Any], secret_id: str | None,
                        description: str = "", region: str | None = None,
                        required_tags: list[str] | None = None) -> str:
        eid, now = _id(), time.time()
        with self.engine.begin() as c:
            c.execute(insert(s.endpoints).values(id=eid, project=project, name=name, type=type_, config=config,
                                                 secret_id=secret_id, description=description, region=region,
                                                 required_tags=required_tags or [], created_at=now, updated_at=now))
        return eid

    def get_endpoint(self, eid: str) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            return _row(c.execute(select(s.endpoints).where(s.endpoints.c.id == eid)).first())

    def list_endpoints(self, project: str | None) -> list[dict[str, Any]]:
        q = select(s.endpoints).order_by(s.endpoints.c.name)
        if project is not None:
            q = q.where(s.endpoints.c.project == project)
        with self.engine.connect() as c:
            return [_row(r) for r in c.execute(q)]  # type: ignore[misc]

    def update_endpoint(self, eid: str, **values: Any) -> None:
        with self.engine.begin() as c:
            c.execute(update(s.endpoints).where(s.endpoints.c.id == eid).values(**values, updated_at=time.time()))

    def delete_endpoint(self, eid: str) -> None:
        with self.engine.begin() as c:
            c.execute(delete(s.endpoints).where(s.endpoints.c.id == eid))

    def endpoint_in_use(self, eid: str) -> bool:
        q = select(func.count()).select_from(s.migrations).where(
            or_(s.migrations.c.source_endpoint_id == eid, s.migrations.c.target_endpoint_id == eid))
        with self.engine.connect() as c:
            return int(c.execute(q).scalar_one()) > 0

    # ------------------------------------------------------------------ migrations
    def create_migration(self, project: str, name: str, mode: str, source_id: str, target_id: str, vm: str,
                         options: dict[str, Any], user_id: str | None) -> str:
        mid, now = _id(), time.time()
        with self.engine.begin() as c:
            c.execute(insert(s.migrations).values(id=mid, project=project, name=name, mode=mode,
                                                  source_endpoint_id=source_id, target_endpoint_id=target_id,
                                                  vm=vm, options=options, state=None, created_by=user_id,
                                                  created_at=now, updated_at=now))
        return mid

    def get_migration(self, mid: str) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            return _row(c.execute(select(s.migrations).where(s.migrations.c.id == mid)).first())

    def list_migrations(self, project: str | None, mode: str | None = None) -> list[dict[str, Any]]:
        q = select(s.migrations).order_by(s.migrations.c.created_at.desc())
        if project is not None:
            q = q.where(s.migrations.c.project == project)
        if mode is not None:
            q = q.where(s.migrations.c.mode == mode)
        with self.engine.connect() as c:
            return [_row(r) for r in c.execute(q)]  # type: ignore[misc]

    def update_migration(self, mid: str, **values: Any) -> None:
        with self.engine.begin() as c:
            c.execute(update(s.migrations).where(s.migrations.c.id == mid).values(**values, updated_at=time.time()))

    def delete_migration(self, mid: str) -> None:
        with self.engine.begin() as c:
            c.execute(delete(s.migrations).where(s.migrations.c.id == mid))

    def save_state(self, mid: str, state: dict[str, Any]) -> None:
        with self.engine.begin() as c:
            c.execute(update(s.migrations).where(s.migrations.c.id == mid).values(state=state, updated_at=time.time()))

    # ------------------------------------------------------------------ executions + queue
    def active_execution(self, mid: str) -> dict[str, Any] | None:
        q = select(s.executions).where(s.executions.c.migration_id == mid, s.executions.c.status.in_(ACTIVE_EXEC))
        with self.engine.connect() as c:
            return _row(c.execute(q).first())

    def create_execution(self, mid: str, *, kind: str, dry_run: bool, user_id: str | None,
                         params: dict[str, Any] | None = None) -> str:
        """Creates a PENDING execution. Refuses a second active execution for the same migration
        (one state blob per migration => one writer)."""
        xid = _id()
        with self.engine.begin() as c:
            busy = c.execute(select(s.executions.c.id).where(
                s.executions.c.migration_id == mid, s.executions.c.status.in_(ACTIVE_EXEC))).first()
            if busy is not None:
                raise RuntimeError(f"migration already has an active execution {busy[0]}")
            c.execute(insert(s.executions).values(id=xid, migration_id=mid, kind=kind, dry_run=dry_run, params=params or {},
                                                  status=ExecStatus.PENDING.value, cancel_requested=False,
                                                  runs=0, created_by=user_id, created_at=time.time()))
        return xid

    def get_execution(self, xid: str) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            return _row(c.execute(select(s.executions).where(s.executions.c.id == xid)).first())

    def list_executions(self, mid: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        q = select(s.executions).order_by(s.executions.c.created_at.desc())
        if mid is not None:
            q = q.where(s.executions.c.migration_id == mid)
        if status is not None:
            q = q.where(s.executions.c.status == status)
        with self.engine.connect() as c:
            return [_row(r) for r in c.execute(q)]  # type: ignore[misc]

    def set_execution(self, xid: str, **values: Any) -> None:
        with self.engine.begin() as c:
            c.execute(update(s.executions).where(s.executions.c.id == xid).values(**values))

    def transition(self, xid: str, frm: tuple[str, ...], to: str, **values: Any) -> bool:
        """Atomic compare-and-set on status. Returns True if this caller won."""
        with self.engine.begin() as c:
            r = c.execute(update(s.executions).where(s.executions.c.id == xid, s.executions.c.status.in_(frm))
                          .values(status=to, **values))
            return r.rowcount == 1

    def claim_execution(self, worker_id: str, lease_s: float, *, region: str | None = None,
                        caps: set[str] | None = None) -> str | None:
        """Claim the oldest QUEUED execution THIS worker can run (region + capabilities)."""
        from ..core.routing import can_run
        with self.engine.connect() as c:
            rows = c.execute(select(s.executions.c.id, s.executions.c.required_caps)
                             .where(s.executions.c.status == ExecStatus.QUEUED.value)
                             .order_by(s.executions.c.created_at).limit(200)).all()
        cands = [xid for xid, req in rows if can_run(region, caps, req)[0]]
        now = time.time()
        for xid in cands:
            with self.engine.begin() as c:
                r = c.execute(update(s.executions)
                              .where(s.executions.c.id == xid, s.executions.c.status == ExecStatus.QUEUED.value)
                              .values(status=ExecStatus.RUNNING.value, lease_owner=worker_id,
                                      lease_expires=now + lease_s, runs=s.executions.c.runs + 1,
                                      started_at=func.coalesce(s.executions.c.started_at, now)))
                if r.rowcount == 1:
                    return xid
        return None

    def renew_lease(self, xid: str, worker_id: str, lease_s: float) -> bool:
        with self.engine.begin() as c:
            r = c.execute(update(s.executions)
                          .where(s.executions.c.id == xid, s.executions.c.lease_owner == worker_id,
                                 s.executions.c.status == ExecStatus.RUNNING.value)
                          .values(lease_expires=time.time() + lease_s))
            return r.rowcount == 1

    def expired_leases(self) -> list[str]:
        q = select(s.executions.c.id).where(s.executions.c.status == ExecStatus.RUNNING.value,
                                            s.executions.c.lease_expires < time.time())
        with self.engine.connect() as c:
            return [r[0] for r in c.execute(q)]

    def cancel_requested(self, xid: str) -> bool:
        with self.engine.connect() as c:
            v = c.execute(select(s.executions.c.cancel_requested).where(s.executions.c.id == xid)).scalar()
        return bool(v)

    # ------------------------------------------------------------------ tasks
    def insert_tasks(self, xid: str, recs: list[TaskRecord]) -> None:
        if not recs:
            return
        with self.engine.begin() as c:
            c.execute(insert(s.tasks), [_task_row(xid, r) for r in recs])

    def list_tasks(self, xid: str) -> list[dict[str, Any]]:
        q = select(s.tasks).where(s.tasks.c.execution_id == xid).order_by(s.tasks.c.seq)
        with self.engine.connect() as c:
            return [_row(r) for r in c.execute(q)]  # type: ignore[misc]

    def transfer_summary(self, xids: list[str]) -> dict[str, dict[str, Any]]:
        """Byte-level disk progress per execution, from its replicate_disk tasks:
        {total_bytes, done_bytes, disks, disks_done, disks_running}. Disks whose size is unknown
        (tasks created before sizes were recorded) count as disks but not as bytes."""
        if not xids:
            return {}
        q = (select(s.tasks.c.execution_id, s.tasks.c.status, s.tasks.c.progress, s.tasks.c.params)
             .where(s.tasks.c.execution_id.in_(xids), s.tasks.c.kind == "replicate_disk"))
        out: dict[str, dict[str, Any]] = {}
        with self.engine.connect() as c:
            for xid, st, pct, params in c.execute(q):
                t = out.setdefault(xid, {"total_bytes": 0, "done_bytes": 0, "disks": 0, "disks_done": 0,
                                         "disks_running": 0})
                size = int((params or {}).get("size_bytes") or 0)
                done = st == "completed"
                t["disks"] += 1
                t["disks_done"] += int(done)
                t["disks_running"] += int(st == "running")
                t["total_bytes"] += size
                t["done_bytes"] += size if done else int(size * min(100.0, float(pct or 0)) / 100)
        return out

    def task_counts(self, xids: list[str]) -> dict[str, dict[str, int]]:
        if not xids:
            return {}
        q = (select(s.tasks.c.execution_id, s.tasks.c.status, func.count())
             .where(s.tasks.c.execution_id.in_(xids)).group_by(s.tasks.c.execution_id, s.tasks.c.status))
        out: dict[str, dict[str, int]] = {}
        with self.engine.connect() as c:
            for xid, st, n in c.execute(q):
                out.setdefault(xid, {})[st] = int(n)
        return out

    # ------------------------------------------------------------------ logs / events
    def add_logs(self, rows: list[dict[str, Any]]) -> None:
        if rows:
            with self.engine.begin() as c:
                c.execute(insert(s.task_logs), rows)

    def get_logs(self, xid: str, task_id: str | None = None, after: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        q = select(s.task_logs).where(s.task_logs.c.execution_id == xid, s.task_logs.c.id > after)
        if task_id:
            q = q.where(s.task_logs.c.task_id == task_id)
        with self.engine.connect() as c:
            return [_row(r) for r in c.execute(q.order_by(s.task_logs.c.id).limit(limit))]  # type: ignore[misc]

    def add_event(self, type_: str, payload: dict[str, Any], *, execution_id: str | None = None,
                  migration_id: str | None = None) -> None:
        with self.engine.begin() as c:
            c.execute(insert(s.events).values(execution_id=execution_id, migration_id=migration_id,
                                              ts=time.time(), type=type_, payload=payload))

    def get_events(self, *, after: int = 0, execution_id: str | None = None, migration_id: str | None = None,
                   limit: int = 500) -> list[dict[str, Any]]:
        q = select(s.events).where(s.events.c.id > after)
        if execution_id:
            q = q.where(s.events.c.execution_id == execution_id)
        if migration_id:
            q = q.where(s.events.c.migration_id == migration_id)
        with self.engine.connect() as c:
            return [_row(r) for r in c.execute(q.order_by(s.events.c.id).limit(limit))]  # type: ignore[misc]

    # ------------------------------------------------------------------ schedules
    def create_schedule(self, mid: str, cron: str, enabled: bool, next_run_at: float | None) -> str:
        sid = _id()
        with self.engine.begin() as c:
            c.execute(insert(s.schedules).values(id=sid, migration_id=mid, cron=cron, enabled=enabled,
                                                 next_run_at=next_run_at, created_at=time.time()))
        return sid

    def get_schedule(self, sid: str) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            return _row(c.execute(select(s.schedules).where(s.schedules.c.id == sid)).first())

    def list_schedules(self, mid: str | None = None) -> list[dict[str, Any]]:
        q = select(s.schedules).order_by(s.schedules.c.created_at)
        if mid is not None:
            q = q.where(s.schedules.c.migration_id == mid)
        with self.engine.connect() as c:
            return [_row(r) for r in c.execute(q)]  # type: ignore[misc]

    def update_schedule(self, sid: str, **values: Any) -> None:
        with self.engine.begin() as c:
            c.execute(update(s.schedules).where(s.schedules.c.id == sid).values(**values))

    def claim_schedule_run(self, sid: str, expected_next: float, new_next: float) -> bool:
        """Advance next_run_at atomically so two conductors never fire the same slot."""
        with self.engine.begin() as c:
            r = c.execute(update(s.schedules).where(s.schedules.c.id == sid, s.schedules.c.next_run_at == expected_next)
                          .values(next_run_at=new_next, last_run_at=time.time()))
            return r.rowcount == 1

    def due_schedules(self, now: float) -> list[dict[str, Any]]:
        q = select(s.schedules).where(s.schedules.c.enabled.is_(True), s.schedules.c.next_run_at <= now)
        with self.engine.connect() as c:
            return [_row(r) for r in c.execute(q)]  # type: ignore[misc]

    def delete_schedule(self, sid: str) -> None:
        with self.engine.begin() as c:
            c.execute(delete(s.schedules).where(s.schedules.c.id == sid))

    # ------------------------------------------------------------------ pools
    def create_pool(self, name: str, *, region: str | None, description: str | None, min_size: int, max_size: int,
                    provision: dict[str, Any] | None) -> str:
        pid, now = _id(), time.time()
        with self.engine.begin() as c:
            c.execute(insert(s.pools).values(id=pid, name=name, region=region, description=description,
                                             min_size=min_size, max_size=max_size, provision=provision,
                                             state={"minions": []}, created_at=now, updated_at=now))
        return pid

    def get_pool(self, pid: str) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            return _row(c.execute(select(s.pools).where(s.pools.c.id == pid)).first())

    def get_pool_by_name(self, name: str) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            return _row(c.execute(select(s.pools).where(s.pools.c.name == name)).first())

    def list_pools(self) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            return [_row(r) for r in c.execute(select(s.pools).order_by(s.pools.c.name))]  # type: ignore[misc]

    def update_pool(self, pid: str, **values: Any) -> None:
        with self.engine.begin() as c:
            c.execute(update(s.pools).where(s.pools.c.id == pid).values(updated_at=time.time(), **values))

    def delete_pool(self, pid: str) -> None:
        with self.engine.begin() as c:
            c.execute(delete(s.enroll_tokens).where(s.enroll_tokens.c.pool_id == pid))
            c.execute(delete(s.pools).where(s.pools.c.id == pid))

    def create_enroll_token(self, pool_id: str | None, ttl_s: float, created_by: str | None) -> tuple[str, float]:
        """Single-use join token; only its hash is stored. Returns (token, expires_at)."""
        import secrets as _secrets
        tok = "mgj_" + _secrets.token_urlsafe(32)
        exp, now = time.time() + ttl_s, time.time()
        with self.engine.begin() as c:
            c.execute(insert(s.enroll_tokens).values(id=_id(), token_hash=_sha(tok), pool_id=pool_id, expires_at=exp,
                                                     created_by=created_by, created_at=now))
        return tok, exp

    def consume_enroll_token(self, tok: str, used_by: str) -> dict[str, Any] | None:
        """Atomically mark a valid, unused, unexpired token used. None when it is not."""
        now = time.time()
        with self.engine.begin() as c:
            r = c.execute(update(s.enroll_tokens)
                          .where(s.enroll_tokens.c.token_hash == _sha(tok), s.enroll_tokens.c.used_at.is_(None),
                                 s.enroll_tokens.c.expires_at > now)
                          .values(used_at=now, used_by=used_by[:256]))
            if r.rowcount != 1:
                return None
            return _row(c.execute(select(s.enroll_tokens).where(s.enroll_tokens.c.token_hash == _sha(tok))).first())

    # ------------------------------------------------------------------ workers
    def worker_heartbeat(self, wid: str, hostname: str, pid: int, current: str | None, *,
                         region: str | None = None, capabilities: list[str] | None = None,
                         export_url: str | None = None, export_fp: str | None = None) -> None:
        now = time.time()
        vals = {"heartbeat_at": now, "current_execution": current, "region": region, "capabilities": capabilities,
                "export_url": export_url, "export_fp": export_fp}
        with self.engine.begin() as c:
            r = c.execute(update(s.workers).where(s.workers.c.id == wid).values(**vals))
            if r.rowcount == 0:
                c.execute(insert(s.workers).values(id=wid, hostname=hostname, pid=pid, started_at=now, **vals))

    def list_workers(self) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            return [_row(r) for r in c.execute(select(s.workers).order_by(s.workers.c.heartbeat_at.desc()))]  # type: ignore[misc]

    def remove_worker(self, wid: str) -> None:
        with self.engine.begin() as c:
            c.execute(delete(s.workers).where(s.workers.c.id == wid))


class DbSharedDisks:
    """engine.shared broker backed by the shared_disks table (atomic first-claim wins)."""

    def __init__(self, repo: Repo):
        self.repo = repo

    def claim(self, cluster: str, shared_id: str, migration: str, size: int) -> dict[str, Any]:
        from sqlalchemy.exc import IntegrityError
        try:
            with self.repo.engine.begin() as c:
                c.execute(insert(s.shared_disks).values(cluster=cluster, shared_id=shared_id, owner=migration,
                                                        status="claimed", size_bytes=size, updated_at=time.time()))
        except IntegrityError:
            pass
        row = self.get(cluster, shared_id)
        assert row is not None
        return row

    def get(self, cluster: str, shared_id: str) -> dict[str, Any] | None:
        with self.repo.engine.connect() as c:
            return _row(c.execute(select(s.shared_disks).where(s.shared_disks.c.cluster == cluster,
                                                               s.shared_disks.c.shared_id == shared_id)).first())

    def update(self, cluster: str, shared_id: str, owner: str, **vals: Any) -> None:
        with self.repo.engine.begin() as c:
            r = c.execute(update(s.shared_disks).where(s.shared_disks.c.cluster == cluster,
                                                       s.shared_disks.c.shared_id == shared_id,
                                                       s.shared_disks.c.owner == owner)
                          .values(updated_at=time.time(), **vals))
            if r.rowcount != 1:
                raise PermissionError(f"shared disk {shared_id} is not owned by {owner}")

    def list(self, cluster: str | None = None) -> list[dict[str, Any]]:
        q = select(s.shared_disks)
        if cluster:
            q = q.where(s.shared_disks.c.cluster == cluster)
        with self.repo.engine.connect() as c:
            return [_row(r) for r in c.execute(q)]  # type: ignore[misc]


def _sha(t: str) -> str:
    import hashlib
    return hashlib.sha256(t.encode()).hexdigest()


def _task_row(xid: str, r: TaskRecord) -> dict[str, Any]:
    return {"execution_id": xid, "id": r.id, "kind": r.kind, "label": r.label, "seq": r.seq,
            "depends_on": list(r.depends_on), "params": dict(r.params), "max_attempts": r.max_attempts,
            "status": r.status.value, "attempts": r.attempts, "progress": r.progress, "message": r.message,
            "error": r.error, "result": _jsonable(r.result), "started_at": r.started_at,
            "finished_at": r.finished_at}


def _jsonable(v: Any) -> Any:
    import json
    return json.loads(json.dumps(v, default=str))


class SqlTaskStore:
    """engine.dag.TaskStore over the tasks table for one execution."""

    def __init__(self, repo: Repo, execution_id: str):
        self.repo = repo
        self.xid = execution_id

    def load_tasks(self) -> list[TaskRecord]:
        return [TaskRecord(id=r["id"], kind=r["kind"], depends_on=list(r["depends_on"]), params=dict(r["params"]),
                           max_attempts=r["max_attempts"], label=r["label"], status=TaskStatus(r["status"]),
                           attempts=r["attempts"], progress=r["progress"], message=r["message"], error=r["error"],
                           result=dict(r["result"] or {}), started_at=r["started_at"],
                           finished_at=r["finished_at"], seq=r["seq"])
                for r in self.repo.list_tasks(self.xid)]

    def save_task(self, rec: TaskRecord) -> None:
        row = _task_row(self.xid, rec)
        row.pop("execution_id")
        tid = row.pop("id")
        with self.repo.engine.begin() as c:
            c.execute(update(s.tasks).where(and_(s.tasks.c.execution_id == self.xid, s.tasks.c.id == tid)).values(**row))

    def add_tasks(self, recs: list[TaskRecord], rewire: dict[str, list[str]]) -> None:
        with self.repo.engine.begin() as c:
            if recs:
                c.execute(insert(s.tasks), [_task_row(self.xid, r) for r in recs])
            for tid, deps in rewire.items():
                c.execute(update(s.tasks).where(s.tasks.c.execution_id == self.xid, s.tasks.c.id == tid)
                          .values(depends_on=list(deps)))


class DbReplicaStore:
    """engine.replica.ReplicaStore over migrations.state (mode = replica)."""

    def __init__(self, repo: Repo, migration_id: str):
        self.repo, self.mid = repo, migration_id

    def load(self) -> dict[str, Any] | None:
        row = self.repo.get_migration(self.mid)
        return row["state"] if row and row.get("state") else None

    def save(self, d: dict[str, Any]) -> None:
        self.repo.save_state(self.mid, _jsonable(d))


class DbStateStore:
    """StateStore-compatible persistence of MigrationState in migrations.state (one migration)."""

    def __init__(self, repo: Repo, migration_id: str):
        self.repo = repo
        self.mid = migration_id

    def load(self, name: str) -> MigrationState | None:
        row = self.repo.get_migration(self.mid)
        return MigrationState.from_dict(row["state"]) if row and row.get("state") else None

    def load_or_new(self, name: str, *, source: str, target: str) -> MigrationState:
        st = self.load(name)
        if st is None:
            return MigrationState(name=name, source=source, target=target)
        if (st.source, st.target) != (source, target):
            raise ValueError(f"state for {name!r} belongs to {st.source}->{st.target}, not {source}->{target}")
        return st

    def save(self, st: MigrationState) -> None:
        self.repo.save_state(self.mid, _jsonable(st.to_dict()))
