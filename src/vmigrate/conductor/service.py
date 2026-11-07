"""Purpose: the conductor — orchestration without I/O against any platform. It turns PENDING
executions into task DAGs and QUEUED work, applies cancellations to work no worker holds yet,
re-queues executions whose worker died (expired lease => resume, not restart), fires due replica
sync schedules (cron, UTC), and prunes dead worker rows.

Usage:
    Conductor(services).run_forever()        # vmigrate-platform conductor
    Conductor(services).tick()               # one pass (tests)
    requeue_for_retry(repo, execution_id)    # API "retry": FAILED/CANCELED -> QUEUED, completed tasks kept

Several conductors may run: every state change is an atomic compare-and-set on executions.status.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from ..db.repo import Repo, SqlTaskStore
from ..engine.dag import ExecStatus, TaskRecord, TaskStatus, reset_for_resume, validate_graph
from ..core.cron import Cron, CronError
from ..engine.flows import migration_dag
from ..engine.replica import delete_dag, deploy_dag, sync_dag
from ..platform.factory import Services

log = logging.getLogger("vmigrate.conductor")

S = ExecStatus


DAGS = {"migration": migration_dag, "replica_sync": sync_dag, "replica_deploy": deploy_dag,
        "replica_delete": delete_dag}


def build_dag(kind: str, dry_run: bool) -> list[TaskRecord]:
    if kind not in DAGS:
        raise NotImplementedError(f"execution kind {kind!r} is not implemented")
    specs = DAGS[kind](dry_run)
    validate_graph(specs)
    return [TaskRecord.from_spec(s, i) for i, s in enumerate(specs)]


def _cancel_pending_tasks(repo: Repo, xid: str) -> None:
    store = SqlTaskStore(repo, xid)
    for t in store.load_tasks():
        if t.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
            t.status = TaskStatus.CANCELED
            store.save_task(t)


def requeue_for_retry(repo: Repo, xid: str) -> int:
    ex = repo.get_execution(xid)
    if ex is None:
        raise KeyError(xid)
    if repo.active_execution(ex["migration_id"]) is not None:
        raise RuntimeError("migration already has an active execution")
    n = reset_for_resume(SqlTaskStore(repo, xid))
    if not repo.transition(xid, (S.FAILED.value, S.CANCELED.value), S.QUEUED.value, error=None,
                           cancel_requested=False, lease_owner=None, lease_expires=None, finished_at=None):
        raise RuntimeError(f"execution {xid} is {ex['status']}; only failed/canceled executions can be retried")
    repo.add_event("execution.queued", {"reason": "retry", "reset_tasks": n}, execution_id=xid,
                   migration_id=ex["migration_id"])
    return n


class Conductor:
    def __init__(self, services: Services):
        self.svc = services
        self.repo = services.repo
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> dict[str, int]:
        stats = {"queued": 0, "canceled": 0, "requeued": 0, "failed": 0, "scheduled": 0}
        repo = self.repo
        # 0) due replica-sync schedules -> new PENDING executions (picked up by step 1 below)
        now = time.time()
        for sch in repo.due_schedules(now):
            try:
                nxt = Cron.parse(sch["cron"]).next_after(now)
            except CronError as e:
                repo.update_schedule(sch["id"], enabled=False, last_skip_reason=f"invalid cron: {e}")
                continue
            if not repo.claim_schedule_run(sch["id"], sch["next_run_at"], nxt):
                continue  # another conductor fired this slot
            mig = repo.get_migration(sch["migration_id"])
            if mig is None or mig["mode"] != "replica":
                repo.update_schedule(sch["id"], enabled=False, last_skip_reason="replica no longer exists")
                continue
            try:
                xid = repo.create_execution(mig["id"], kind="replica_sync", dry_run=False, user_id=None,
                                            params={"schedule_id": sch["id"]})
            except RuntimeError as e:   # a sync/deploy is still running: skip this slot, never pile up
                repo.update_schedule(sch["id"], last_skip_reason=f"skipped at {time.strftime('%Y-%m-%d %H:%M', time.gmtime(now))}Z: {e}")
                continue
            repo.update_schedule(sch["id"], last_execution_id=xid, last_skip_reason=None)
            repo.add_event("execution.created", {"dry_run": False, "by": f"schedule {sch['cron']}"},
                           execution_id=xid, migration_id=mig["id"])
            stats["scheduled"] += 1
        # 1) PENDING -> build DAG -> QUEUED (or CANCELED when canceled before it ever ran)
        for ex in repo.list_executions(status=S.PENDING.value):
            xid = ex["id"]
            if ex["cancel_requested"]:
                if repo.transition(xid, (S.PENDING.value,), S.CANCELED.value, finished_at=time.time()):
                    stats["canceled"] += 1
                    repo.add_event("execution.canceled", {}, execution_id=xid, migration_id=ex["migration_id"])
                continue
            try:
                recs = build_dag(ex["kind"], ex["dry_run"])
            except Exception as e:  # noqa: BLE001 - surface as a failed execution
                if repo.transition(xid, (S.PENDING.value,), S.FAILED.value, error=f"{type(e).__name__}: {e}",
                                   finished_at=time.time()):
                    stats["failed"] += 1
                    repo.add_event("execution.failed", {"error": str(e)}, execution_id=xid,
                                   migration_id=ex["migration_id"])
                continue
            if not repo.list_tasks(xid):
                repo.insert_tasks(xid, recs)
            if repo.transition(xid, (S.PENDING.value,), S.QUEUED.value):
                stats["queued"] += 1
                repo.add_event("execution.queued", {"tasks": len(recs), "dry_run": ex["dry_run"]},
                               execution_id=xid, migration_id=ex["migration_id"])
        # 2) cancel requested while still QUEUED (no worker yet)
        for ex in repo.list_executions(status=S.QUEUED.value):
            if ex["cancel_requested"] and repo.transition(ex["id"], (S.QUEUED.value,), S.CANCELED.value,
                                                          finished_at=time.time()):
                _cancel_pending_tasks(repo, ex["id"])
                stats["canceled"] += 1
                repo.add_event("execution.canceled", {}, execution_id=ex["id"], migration_id=ex["migration_id"])
        # 3) worker died: lease expired -> RUNNING tasks back to PENDING, execution back to QUEUED
        for xid in repo.expired_leases():
            ex = repo.get_execution(xid) or {}
            if repo.transition(xid, (S.RUNNING.value,), S.QUEUED.value, lease_owner=None, lease_expires=None):
                store = SqlTaskStore(repo, xid)
                for t in store.load_tasks():
                    if t.status == TaskStatus.RUNNING:
                        t.status = TaskStatus.PENDING
                        store.save_task(t)
                stats["requeued"] += 1
                log.warning("execution %s: worker %s lease expired; re-queued for resume", xid, ex.get("lease_owner"))
                repo.add_event("execution.queued", {"reason": "lease_expired", "previous_worker": ex.get("lease_owner")},
                               execution_id=xid, migration_id=ex.get("migration_id"))
        # 4) prune worker rows that stopped heart-beating long ago
        dead = time.time() - 10 * self.svc.settings.lease_seconds
        for w in repo.list_workers():
            if w["heartbeat_at"] < dead:
                repo.remove_worker(w["id"])
        return stats

    def run_forever(self) -> None:
        log.info("conductor started (poll %.1fs)", self.svc.settings.poll_seconds)
        while not self._stop.is_set():
            try:
                s: dict[str, Any] = self.tick()
                if any(s.values()):
                    log.info("conductor tick: %s", s)
            except Exception:  # noqa: BLE001 - keep orchestrating
                log.exception("conductor tick failed")
            self._stop.wait(self.svc.settings.poll_seconds)
