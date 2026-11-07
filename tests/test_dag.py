"""Purpose: unit tests for the generic task-DAG engine: ordering, bounded parallelism, runtime
expansion, retries (transient only), failure blocking, cancel, resume, graph validation.

Usage: pytest tests/test_dag.py
"""

from __future__ import annotations

import threading
import time

import pytest

from vmigrate.core.errors import SafetyGateError, TransientProviderError
from vmigrate.engine.dag import (
    DagError,
    DagRunner,
    ExecStatus,
    MemoryTaskStore,
    TaskOutcome,
    TaskSpec,
    TaskStatus,
    reset_for_resume,
    validate_graph,
)


def st(store: MemoryTaskStore) -> dict[str, str]:
    return {t.id: t.status.value for t in store.load_tasks()}


def test_runs_in_dependency_order():
    order: list[str] = []
    store = MemoryTaskStore([TaskSpec("a", "k"), TaskSpec("b", "k", ["a"]), TaskSpec("c", "k", ["b"])])
    status = DagRunner(store, {"k": lambda ctx: order.append(ctx.task_id)}).run()
    assert status == ExecStatus.COMPLETED and order == ["a", "b", "c"]


def test_parallelism_is_bounded():
    live, peak, lock = [0], [0], threading.Lock()

    def h(ctx):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock:
            live[0] -= 1

    store = MemoryTaskStore([TaskSpec(f"t{i}", "k") for i in range(8)])
    assert DagRunner(store, {"k": h}, max_parallel=3).run() == ExecStatus.COMPLETED
    assert peak[0] == 3


def test_expansion_rewires_dependents():
    seen: list[str] = []

    def parent(ctx):
        return TaskOutcome(children=[TaskSpec(f"child{i}", "child") for i in range(3)])

    def child(ctx):
        seen.append(ctx.task_id)

    def after(ctx):
        assert sorted(seen) == ["child0", "child1", "child2"]  # all children ran before the dependent
        seen.append("after")

    store = MemoryTaskStore([TaskSpec("p", "parent"), TaskSpec("z", "after", ["p"])])
    assert DagRunner(store, {"parent": parent, "child": child, "after": after}).run() == ExecStatus.COMPLETED
    assert seen[-1] == "after"
    z = next(t for t in store.load_tasks() if t.id == "z")
    assert set(z.depends_on) == {"p", "child0", "child1", "child2"}


def test_transient_errors_retry_with_backoff_but_gates_do_not():
    calls = {"flaky": 0, "gate": 0}
    sleeps: list[float] = []

    def flaky(ctx):
        calls["flaky"] += 1
        if calls["flaky"] < 3:
            raise TransientProviderError("blip")

    def gate(ctx):
        calls["gate"] += 1
        raise SafetyGateError("disk count mismatch")

    store = MemoryTaskStore([TaskSpec("f", "flaky", max_attempts=3)])
    r = DagRunner(store, {"flaky": flaky}, retry_on=(TransientProviderError,), sleep=sleeps.append)
    assert r.run() == ExecStatus.COMPLETED and calls["flaky"] == 3 and sleeps == [2.0, 4.0]

    store = MemoryTaskStore([TaskSpec("g", "gate", max_attempts=5)])
    assert DagRunner(store, {"gate": gate}, retry_on=(TransientProviderError,), sleep=sleeps.append).run() \
        == ExecStatus.FAILED
    assert calls["gate"] == 1


def test_failure_blocks_dependents_and_resume_skips_completed():
    runs: list[str] = []
    boom = [True]

    def h(ctx):
        runs.append(ctx.task_id)
        if ctx.task_id == "b" and boom[0]:
            raise RuntimeError("b broke")

    store = MemoryTaskStore([TaskSpec("a", "k"), TaskSpec("b", "k", ["a"]), TaskSpec("c", "k", ["b"])])
    assert DagRunner(store, {"k": h}).run() == ExecStatus.FAILED
    assert st(store) == {"a": "completed", "b": "failed", "c": "pending"}
    boom[0] = False
    assert reset_for_resume(store) == 1
    assert DagRunner(store, {"k": h}).run() == ExecStatus.COMPLETED
    assert runs == ["a", "b", "b", "c"]  # "a" was not re-run


def test_cancel_stops_running_and_pending():
    started = threading.Event()
    flag = [False]

    def slow(ctx):
        started.set()
        for _ in range(200):
            ctx.check_cancel()
            time.sleep(0.01)

    store = MemoryTaskStore([TaskSpec("a", "slow"), TaskSpec("b", "slow", ["a"])])
    runner = DagRunner(store, {"slow": slow}, cancel_check=lambda: flag[0])
    t = threading.Thread(target=lambda: setattr(t, "status", runner.run()))
    t.start()
    started.wait(2)
    flag[0] = True
    t.join(5)
    assert t.status == ExecStatus.CANCELED  # type: ignore[attr-defined]
    assert st(store) == {"a": "canceled", "b": "canceled"}


def test_graph_validation():
    with pytest.raises(DagError, match="cycle"):
        validate_graph([TaskSpec("a", "k", ["b"]), TaskSpec("b", "k", ["a"])])
    with pytest.raises(DagError, match="unknown"):
        validate_graph([TaskSpec("a", "k", ["nope"])])
    with pytest.raises(DagError, match="duplicate"):
        validate_graph([TaskSpec("a", "k"), TaskSpec("a", "k")])


def test_progress_is_persisted_and_bounded():
    def h(ctx):
        ctx.progress(150, "over", force=True)

    store = MemoryTaskStore([TaskSpec("a", "k")])
    events: list[tuple[str, float]] = []
    DagRunner(store, {"k": h}, on_event=lambda e, r: events.append((e, r.progress))).run()
    assert ("task.progress", 100.0) in events
    assert store.load_tasks()[0].status == TaskStatus.COMPLETED
