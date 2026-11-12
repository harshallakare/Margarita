"""Purpose: the worker — claims QUEUED executions, builds the providers from the endpoint rows
(credentials decrypted only here, in memory), runs the task DAG, and streams task status,
progress, logs and events into the DB. One execution at a time per worker process; scale out by
running more workers. A heartbeat thread renews the lease; if the lease is lost (conductor
re-queued it), the run is cancelled locally so two workers never drive the same migration.

Usage:
    Worker(services).run_forever()          # vmigrate-platform worker
    Worker(services).tick()                 # claim + run one execution if any (tests)

Dry-run executions run with RunContext(dry_run=True): every mutating provider method raises
DryRunViolation, and the dry-run DAG contains no mutating task in the first place.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from typing import Any

from ..core.context import RunContext
from ..core.errors import TransientProviderError, is_transient
from ..db.repo import SqlTaskStore
from ..engine import dag as dagmod
from ..engine.dag import BACKOFF_MAX, DagRunner, ExecStatus, TaskRecord, TaskStatus
from ..engine.flows import migration_handlers, record_failure
from ..platform.factory import Services

log = logging.getLogger("vmigrate.worker")


class DbLogHandler(logging.Handler):
    """Captures vmigrate.* log records into task_logs (tagged with the task the thread is running).
    Buffered; flushed every second / 50 records / at the end of the run."""

    def __init__(self, svc: Services, xid: str, level: int = logging.INFO):
        super().__init__(level)
        self.svc, self.xid = svc, xid
        self.buf: list[dict[str, Any]] = []
        self.last = time.monotonic()
        self._l = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            msg = str(record.msg)
        if record.exc_info and record.exc_info[1] is not None:
            import traceback
            msg += "\n" + "".join(traceback.format_exception(*record.exc_info))[-12000:]
        row = {"execution_id": self.xid, "task_id": getattr(dagmod.current, "task_id", None), "ts": record.created,
               "level": record.levelname, "message": msg[:20000], "logger": record.name[:128]}
        with self._l:
            self.buf.append(row)
            due = len(self.buf) >= 50 or time.monotonic() - self.last >= 1.0
        if due:
            self.flush()

    def flush(self) -> None:
        with self._l:
            rows, self.buf = self.buf, []
            self.last = time.monotonic()
        if rows:
            try:
                self.svc.repo.add_logs(rows)
            except Exception:  # noqa: BLE001 - logging must never break a migration
                pass


class Worker:
    def __init__(self, services: Services, worker_id: str | None = None, *, region: str | None = None,
                 capabilities: set[str] | None = None):
        from ..core.routing import detect_capabilities
        self.svc = services
        self.repo = services.repo
        self.region = region if region is not None else (os.environ.get("VMIGRATE_WORKER_REGION") or None)
        self.capabilities = capabilities if capabilities is not None else detect_capabilities()
        self.host = socket.gethostname()
        self.id = worker_id or f"{self.host}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._stop = threading.Event()
        self.current: str | None = None

    def stop(self) -> None:
        self._stop.set()

    def heartbeat(self) -> None:
        self.repo.worker_heartbeat(self.id, self.host, os.getpid(), self.current, region=self.region,
                                   capabilities=sorted(self.capabilities))

    def tick(self) -> str | None:
        self.heartbeat()
        xid = self.repo.claim_execution(self.id, self.svc.settings.lease_seconds, region=self.region,
                                        caps=self.capabilities)
        if xid is None:
            return None
        self.run_execution(xid)
        return xid

    def run_forever(self) -> None:
        log.info("worker %s started", self.id)
        while not self._stop.is_set():
            try:
                if self.tick() is None:
                    self._stop.wait(self.svc.settings.poll_seconds)
            except Exception:  # noqa: BLE001
                log.exception("worker tick failed")
                self._stop.wait(self.svc.settings.poll_seconds)
        self.repo.remove_worker(self.id)

    # ------------------------------------------------------------------ one execution
    def run_execution(self, xid: str) -> ExecStatus:
        repo, st = self.repo, self.svc.settings
        ex = repo.get_execution(xid)
        assert ex is not None
        mig = repo.get_migration(ex["migration_id"])
        mid = ex["migration_id"]
        self.current = xid
        lost = threading.Event()
        done = threading.Event()
        dag_ref: dict[str, DagRunner] = {}

        def beat() -> None:
            while not done.wait(max(1.0, st.lease_seconds / 3)):
                try:
                    if not lost.is_set() and not repo.renew_lease(xid, self.id, st.lease_seconds):
                        cur = repo.get_execution(xid) or {}
                        if cur.get("status") == ExecStatus.CANCELED.value:
                            log.warning("execution %s was force-canceled; stopping locally", xid)
                        else:
                            log.error("execution %s: lease lost; stopping locally", xid)
                        lost.set()
                        if "dag" in dag_ref:
                            dag_ref["dag"].cancel()
                    # keep heart-beating while winding down: new runs of this migration wait for it
                    self.heartbeat()
                except Exception:  # noqa: BLE001
                    log.exception("lease renewal failed")

        hb = threading.Thread(target=beat, name=f"lease-{xid[:8]}", daemon=True)
        hb.start()
        # VMIGRATE_EXEC_LOG_LEVEL=DEBUG also records provider API call traces in the run log
        lvl = logging.getLevelName(os.environ.get("VMIGRATE_EXEC_LOG_LEVEL", "INFO").upper())
        lvl = lvl if isinstance(lvl, int) else logging.INFO
        handler = DbLogHandler(self.svc, xid, lvl)
        vlog = logging.getLogger("vmigrate")
        vlog.addHandler(handler)
        if vlog.level == logging.NOTSET or vlog.level > lvl:
            vlog.setLevel(lvl)

        last_cancel = [0.0, False]

        def cancel_check() -> bool:
            now = time.monotonic()
            if now - last_cancel[0] >= 2.0:
                last_cancel[0] = now
                last_cancel[1] = repo.cancel_requested(xid)
            return bool(last_cancel[1]) or lost.is_set()

        def on_event(ev: str, rec: TaskRecord) -> None:
            repo.add_event(ev, {"task_id": rec.id, "kind": rec.kind, "label": rec.label, "status": rec.status.value,
                                "progress": round(rec.progress, 1), "message": rec.message, "error": rec.error},
                           execution_id=xid, migration_id=mid)

        status, error = ExecStatus.FAILED, None
        repo.add_event("execution.running", {"worker": self.id, "dry_run": ex["dry_run"]},
                       execution_id=xid, migration_id=mid)
        try:
            if mig is None:
                raise RuntimeError("migration row disappeared")
            ctx = RunContext(dry_run=bool(ex["dry_run"]), run_id=xid, logger=logging.getLogger(f"vmigrate.exec.{xid[:8]}"))
            kind = ex["kind"]
            runner: Any
            if kind == "migration":
                runner = self.svc.build_runner(mig, ctx)
                handlers = migration_handlers(runner)
            elif kind in ("replica_sync", "replica_deploy", "replica_delete", "replica_cutover"):
                runner = self.svc.build_replica_runner(mig, ctx)
                handlers = {"replica_sync": runner.sync_handlers,
                            "replica_deploy": lambda: runner.deploy_handlers(dict(ex.get("params") or {})),
                            "replica_delete": runner.delete_handlers,
                            "replica_cutover": lambda: runner.cutover_handlers(dict(ex.get("params") or {}))}[kind]()
            else:
                raise RuntimeError(f"unknown execution kind {kind!r}")
            if kind != "migration":
                try:
                    runner.begin(kind)        # reuse (sync) or remove (deploy/cutover/delete) a kept helper VM
                except Exception:
                    runner.source.close()
                    runner.target.close()
                    raise
            try:
                dag = DagRunner(SqlTaskStore(repo, xid), handlers,
                                max_parallel=runner.engine.max_parallel_disks, cancel_check=cancel_check,
                                retry_on=(TransientProviderError,), retry_if=is_transient,
                                backoff_max=BACKOFF_MAX, on_event=on_event)
                dag_ref["dag"] = dag
                status = dag.run()
                if status != ExecStatus.COMPLETED:
                    failed = [t for t in SqlTaskStore(repo, xid).load_tasks() if t.status == TaskStatus.FAILED]
                    error = (f"{failed[0].id}: {failed[0].error}" if failed else
                             "canceled" if status == ExecStatus.CANCELED else "incomplete")
                    if kind == "migration":
                        record_failure(runner, error, cleanup=False if status == ExecStatus.CANCELED else None)
                    else:
                        runner.record_failure(kind, error)
            finally:
                # Cleanup (helper VM, sessions) must never replace the run's real outcome. Seen live: a
                # network cut failed the copy, then the helper cleanup failed too and its error became
                # the execution's error. A leftover helper is removed when the execution is resumed.
                def quietly(what: str, fn: Any) -> None:
                    nonlocal error
                    try:
                        fn()
                    except Exception as e:  # noqa: BLE001
                        log.warning("execution %s: %s failed: %s", xid, what, e)
                        note = f"cleanup ({what}) incomplete: {e}"
                        error = f"{error}; {note}" if error else None
                        repo.add_event("execution.cleanup_failed", {"what": what, "error": str(e)[:2000]},
                                       execution_id=xid, migration_id=mid)
                if kind == "migration":
                    quietly("source", runner.source.close)
                    quietly("target", runner.target.close)
                else:
                    quietly("source + target", lambda: runner.finish(kind, status == ExecStatus.COMPLETED))
        except Exception as e:  # noqa: BLE001 - provider build / config errors
            status, error = ExecStatus.FAILED, f"{type(e).__name__}: {e}"
            log.exception("execution %s failed before/outside the task graph", xid)
        finally:
            done.set()
            hb.join(timeout=5)
            vlog.removeHandler(handler)
            handler.flush()
            self.current = None
        if lost.is_set():
            self.heartbeat()   # current_execution cleared: a new run of this migration may start
            return status  # the conductor re-queued it, or it was force-canceled
        if not repo.transition(xid, (ExecStatus.RUNNING.value,), status.value, error=error, finished_at=time.time(),
                               lease_owner=None, lease_expires=None):
            log.info("execution %s was finalized elsewhere (force-canceled); its outcome is kept", xid)
            self.current = None
            self.heartbeat()
            return status
        repo.add_event(f"execution.{status.value}", {"error": error}, execution_id=xid, migration_id=mid)
        log.info("execution %s -> %s%s", xid, status.value, f" ({error})" if error else "")
        return status
