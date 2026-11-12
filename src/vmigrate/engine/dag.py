"""Purpose: the generic task-DAG engine every execution runs on (migrations, replica syncs, deploys).
It is pure orchestration: no provider, DB or platform knowledge. Persistence goes through the
TaskStore protocol, so the same engine runs against an in-memory store in tests and the SQL store
in the worker.

Usage:
    store = MemoryTaskStore([TaskSpec("a", "inspect"), TaskSpec("b", "copy", depends_on=["a"])])
    runner = DagRunner(store, handlers={"inspect": fn_a, "copy": fn_b}, max_parallel=4)
    status = runner.run()          # -> ExecStatus.COMPLETED / FAILED / CANCELED

Semantics:
  * A task runs once every dependency is COMPLETED. Ready tasks run concurrently (bounded).
  * A handler gets a TaskContext (params, progress(), cancel check, logger) and returns
    TaskOutcome or None. TaskOutcome.children EXPANDS the graph at runtime (e.g. "deploy disks"
    returns one "replicate_disk" task per disk): children depend on the parent, and every task
    that depended on the parent now also depends on all the children.
  * Retries: only errors whose class is in `retry_on`, or for which `retry_if(e)` is true, are
    retried (exponential backoff capped at backoff_max, up to TaskSpec.max_attempts; a cancel ends
    the wait). Safety-gate errors are never retried.
  * Resume: COMPLETED tasks are never re-run. reset_for_resume() turns FAILED/CANCELED/RUNNING
    back into PENDING so a re-queued execution continues where it stopped.
  * Cancel: cancel_check() polled between tasks and inside handlers via ctx.check_cancel();
    running handlers raise TaskCanceled at their next check, pending tasks become CANCELED.
  * A failed task leaves its dependents PENDING (blocked); the execution ends FAILED.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol

from ..core import cancel
from ..core.errors import OperationCanceled

log = logging.getLogger("vmigrate.dag")

# Disk copy tasks resume from their durable watermark, so retrying them is cheap and safe. 8 attempts
# with backoff 2^n s capped at 5 min ride out a network outage of ~8-10 minutes (seen live: a cut
# mid-copy used to fail the task after 3 attempts within ~6 s).
COPY_ATTEMPTS = 8
BACKOFF_MAX = 300.0


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class ExecStatus(StrEnum):
    PENDING = "pending"      # created by the API, DAG not built yet
    QUEUED = "queued"        # DAG built by the conductor, waiting for a worker
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


TERMINAL_EXEC = {ExecStatus.COMPLETED, ExecStatus.FAILED, ExecStatus.CANCELED}


class TaskCanceled(OperationCanceled):
    """Raised inside a handler when the execution was canceled."""


class DagError(ValueError):
    """Invalid graph (cycle, unknown dependency, duplicate id)."""


@dataclass
class TaskSpec:
    id: str
    kind: str
    depends_on: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    max_attempts: int = 1
    label: str = ""


@dataclass
class TaskRecord:
    id: str
    kind: str
    depends_on: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    max_attempts: int = 1
    label: str = ""
    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0
    progress: float = 0.0           # 0..100
    message: str = ""
    error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    started_at: float | None = None
    finished_at: float | None = None
    seq: int = 0                    # insertion order (stable display / scheduling order)

    @classmethod
    def from_spec(cls, s: TaskSpec, seq: int = 0) -> TaskRecord:
        return cls(id=s.id, kind=s.kind, depends_on=list(s.depends_on), params=dict(s.params),
                   max_attempts=s.max_attempts, label=s.label or s.id, seq=seq)


@dataclass
class TaskOutcome:
    result: dict[str, Any] = field(default_factory=dict)
    children: list[TaskSpec] = field(default_factory=list)
    message: str = ""


class TaskStore(Protocol):
    def load_tasks(self) -> list[TaskRecord]: ...
    def save_task(self, rec: TaskRecord) -> None: ...
    def add_tasks(self, recs: list[TaskRecord], rewire: dict[str, list[str]]) -> None:
        """Insert `recs` and replace depends_on for the task ids in `rewire`, atomically."""


class MemoryTaskStore:
    def __init__(self, specs: Iterable[TaskSpec] = ()):
        self._lock = threading.Lock()
        self.tasks: dict[str, TaskRecord] = {}
        for i, s in enumerate(specs):
            self.tasks[s.id] = TaskRecord.from_spec(s, i)

    def load_tasks(self) -> list[TaskRecord]:
        with self._lock:
            return [replace(t, depends_on=list(t.depends_on), params=dict(t.params), result=dict(t.result))
                    for t in sorted(self.tasks.values(), key=lambda t: t.seq)]

    def save_task(self, rec: TaskRecord) -> None:
        with self._lock:
            self.tasks[rec.id] = replace(rec)

    def add_tasks(self, recs: list[TaskRecord], rewire: dict[str, list[str]]) -> None:
        with self._lock:
            for r in recs:
                self.tasks[r.id] = replace(r)
            for tid, deps in rewire.items():
                self.tasks[tid].depends_on = list(deps)


def validate_graph(tasks: Iterable[TaskRecord | TaskSpec]) -> None:
    by_id: dict[str, list[str]] = {}
    for t in tasks:
        if t.id in by_id:
            raise DagError(f"duplicate task id {t.id!r}")
        by_id[t.id] = list(t.depends_on)
    for tid, deps in by_id.items():
        for d in deps:
            if d not in by_id:
                raise DagError(f"task {tid!r} depends on unknown task {d!r}")
    # Kahn's algorithm for cycle detection
    indeg = {t: len(d) for t, d in by_id.items()}
    users: dict[str, list[str]] = {t: [] for t in by_id}
    for t, deps in by_id.items():
        for d in deps:
            users[d].append(t)
    ready = [t for t, n in indeg.items() if n == 0]
    seen = 0
    while ready:
        t = ready.pop()
        seen += 1
        for u in users[t]:
            indeg[u] -= 1
            if indeg[u] == 0:
                ready.append(u)
    if seen != len(by_id):
        raise DagError("task graph has a cycle: " + ", ".join(sorted(t for t, n in indeg.items() if n > 0)))


def reset_for_resume(store: TaskStore) -> int:
    """FAILED / CANCELED / RUNNING (crashed worker) -> PENDING. Returns how many were reset."""
    n = 0
    for t in store.load_tasks():
        if t.status in (TaskStatus.FAILED, TaskStatus.CANCELED, TaskStatus.RUNNING):
            t.status, t.error, t.message = TaskStatus.PENDING, None, ""
            t.attempts = 0
            store.save_task(t)
            n += 1
    return n


def reset_after_cleanup(store: TaskStore) -> int:
    """The failure cleanup deleted the run's target disks/VM (MigrationState.target_cleaned): every
    task must run again on retry
    (COMPLETED ones too: "disks created/copied" no longer holds). Validate/inspect are cheap and
    the snapshot task reuses the kept source snapshot. Returns how many were reset."""
    n = 0
    for t in store.load_tasks():
        if t.status != TaskStatus.PENDING:
            t.status, t.error, t.message, t.progress = TaskStatus.PENDING, None, "", 0.0
            t.attempts = 0
            store.save_task(t)
            n += 1
    return n


class TaskContext:
    def __init__(self, runner: DagRunner, rec: TaskRecord):
        self._runner = runner
        self.rec = rec
        self.task_id = rec.id
        self.params = rec.params
        self.logger = logging.getLogger(f"vmigrate.task.{rec.kind}")
        self._last_save = 0.0

    def check_cancel(self) -> None:
        if self._runner.cancelled():
            raise TaskCanceled(f"task {self.task_id} canceled")

    def progress(self, pct: float, message: str = "", *, force: bool = False) -> None:
        """Report progress (0..100). Persisted at most every `progress_interval` seconds."""
        self.check_cancel()
        self.rec.progress = max(0.0, min(100.0, float(pct)))
        if message:
            self.rec.message = message
        now = time.monotonic()
        if force or now - self._last_save >= self._runner.progress_interval:
            self._last_save = now
            self._runner._persist(self.rec, event="task.progress")

    def result(self, of_task: str) -> dict[str, Any]:
        """Result dict of an (already completed) task in this execution."""
        return self._runner.results.get(of_task, {})


Handler = Callable[[TaskContext], TaskOutcome | None]
EventSink = Callable[[str, TaskRecord], None]

# thread-local: which task the current thread is executing (used by the per-task log handler)
current = threading.local()


class DagRunner:
    def __init__(
        self,
        store: TaskStore,
        handlers: dict[str, Handler],
        *,
        max_parallel: int = 4,
        cancel_check: Callable[[], bool] = lambda: False,
        retry_on: tuple[type[BaseException], ...] = (),
        retry_if: Callable[[BaseException], bool] | None = None,
        backoff_base: float = 2.0,
        backoff_max: float = 120.0,
        on_event: EventSink | None = None,
        progress_interval: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.store = store
        self.handlers = handlers
        self.max_parallel = max(1, max_parallel)
        self._cancel_check = cancel_check
        self._cancelled = threading.Event()
        self.retry_on = retry_on
        self.retry_if = retry_if
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.on_event = on_event
        self.progress_interval = progress_interval
        self.sleep = sleep
        self._lock = threading.RLock()
        self.results: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ helpers
    def cancelled(self) -> bool:
        if not self._cancelled.is_set() and self._cancel_check():
            self._cancelled.set()
        return self._cancelled.is_set()

    def cancel(self) -> None:
        self._cancelled.set()

    def _persist(self, rec: TaskRecord, event: str | None = None) -> None:
        with self._lock:
            self.store.save_task(rec)
        if event and self.on_event:
            try:
                self.on_event(event, rec)
            except Exception:  # an event sink must never break the run
                log.exception("event sink failed")

    def _run_one(self, rec: TaskRecord) -> TaskOutcome | None:
        handler = self.handlers.get(rec.kind)
        if handler is None:
            raise DagError(f"no handler registered for task kind {rec.kind!r}")
        current.task_id = rec.id
        try:
            while True:
                rec.attempts += 1
                try:
                    with cancel.bound(self.cancelled):   # waits/commands in this thread stop on cancel
                        return handler(TaskContext(self, rec))
                except OperationCanceled:
                    raise
                except Exception as e:
                    retryable = isinstance(e, self.retry_on) or (self.retry_if is not None and self.retry_if(e))
                    if not retryable or rec.attempts >= rec.max_attempts or self.cancelled():
                        raise
                    delay = min(self.backoff_max, self.backoff_base ** rec.attempts)
                    rec.message = (f"attempt {rec.attempts}/{rec.max_attempts} failed ({type(e).__name__}: "
                                   f"{str(e)[:300]}); retrying in {delay:.0f}s")
                    log.warning("task %s: %s", rec.id, rec.message)
                    self._persist(rec, event="task.retry")
                    left = delay
                    while left > 0 and not self.cancelled():   # a cancel ends the wait, not just the next try
                        step = min(1.0, left)
                        self.sleep(step)
                        left -= step
                    if self.cancelled():
                        raise
        finally:
            current.task_id = None

    def _expand(self, parent: TaskRecord, children: list[TaskSpec]) -> None:
        tasks = {t.id: t for t in self.store.load_tasks()}
        # A parent re-run after reset_after_cleanup returns the same children again: they are
        # already in the graph (reset to PENDING, still wired), so only genuinely new ones are added.
        children = [c for c in children if c.id not in tasks]
        if not children:
            return
        base = max((t.seq for t in tasks.values()), default=0) + 1
        new = []
        for i, c in enumerate(children):
            deps = list(dict.fromkeys([parent.id, *c.depends_on]))
            new.append(TaskRecord.from_spec(replace(c, depends_on=deps), base + i))
        child_ids = [c.id for c in new]
        rewire = {t.id: list(dict.fromkeys([*t.depends_on, *child_ids]))
                  for t in tasks.values() if parent.id in t.depends_on and t.id not in child_ids}
        validate_graph([*(replace(t, depends_on=rewire.get(t.id, t.depends_on)) for t in tasks.values()), *new])
        with self._lock:
            self.store.add_tasks(new, rewire)

    # ------------------------------------------------------------------ driver
    def run(self) -> ExecStatus:
        tasks = self.store.load_tasks()
        validate_graph(tasks)
        for t in tasks:
            if t.status == TaskStatus.COMPLETED:
                self.results[t.id] = t.result
        failed = False
        running: dict[Future[TaskOutcome | None], TaskRecord] = {}
        with ThreadPoolExecutor(max_workers=self.max_parallel, thread_name_prefix="task") as pool:
            while True:
                tasks = {t.id: t for t in self.store.load_tasks()}
                busy = {r.id for r in running.values()}
                if not failed and not self.cancelled():
                    ready = [t for t in sorted(tasks.values(), key=lambda t: t.seq)
                             if t.status == TaskStatus.PENDING and t.id not in busy
                             and all(tasks[d].status == TaskStatus.COMPLETED for d in t.depends_on)]
                    for t in ready[: self.max_parallel - len(running)]:
                        t.status, t.started_at, t.error = TaskStatus.RUNNING, time.time(), None
                        self._persist(t, event="task.started")
                        running[pool.submit(self._run_one, t)] = t
                if not running:
                    break
                done, _ = wait(list(running), timeout=1.0, return_when=FIRST_COMPLETED)
                for f in done:
                    rec = running.pop(f)
                    rec.finished_at = time.time()
                    exc = f.exception()
                    if exc is None:
                        out = f.result() or TaskOutcome()
                        if out.children:
                            self._expand(rec, out.children)
                        rec.status, rec.progress, rec.result = TaskStatus.COMPLETED, 100.0, dict(out.result)
                        rec.message = out.message or rec.message
                        self.results[rec.id] = rec.result
                        self._persist(rec, event="task.completed")
                    elif isinstance(exc, OperationCanceled):
                        rec.status, rec.error = TaskStatus.CANCELED, str(exc)
                        self._persist(rec, event="task.canceled")
                    else:
                        failed = True
                        rec.status, rec.error = TaskStatus.FAILED, f"{type(exc).__name__}: {exc}"
                        log.error("task %s (%s) failed: %s", rec.id, rec.kind, rec.error, exc_info=exc)
                        self._persist(rec, event="task.failed")
        final = {t.id: t for t in self.store.load_tasks()}
        if self.cancelled():
            for t in final.values():
                if t.status == TaskStatus.PENDING:
                    t.status = TaskStatus.CANCELED
                    self._persist(t, event="task.canceled")
            return ExecStatus.CANCELED
        if failed or any(t.status != TaskStatus.COMPLETED for t in final.values()):
            return ExecStatus.FAILED
        return ExecStatus.COMPLETED
