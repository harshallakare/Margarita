"""Purpose: cancellation — waits and external commands in a task thread stop within about a second
of a cancel (core.cancel), and force-cancel marks a run CANCELED at once without waiting for its
worker, while a new run of the same migration waits until that worker has stopped.

Usage: pytest tests/test_cancel.py
"""

from __future__ import annotations

import sys
import threading
import time

import pytest

from vmigrate.core import cancel
from vmigrate.core.errors import OperationCanceled
from vmigrate.core.retry import poll_until
from vmigrate.db.repo import SqlTaskStore
from vmigrate.engine.dag import TaskStatus

from .test_platform import plat  # noqa: F401  (fixture)


def _flag_after(seconds: float):
    ev = threading.Event()
    threading.Timer(seconds, ev.set).start()
    return ev.is_set


def test_waits_and_commands_stop_soon_after_a_cancel():
    t0 = time.monotonic()
    with cancel.bound(_flag_after(0.2)), pytest.raises(OperationCanceled):
        cancel.sleep(30)
    with cancel.bound(_flag_after(0.2)), pytest.raises(OperationCanceled):
        poll_until(lambda: 0, lambda x: False, timeout=60, interval=10, what="never")
    with cancel.bound(_flag_after(0.2)), pytest.raises(OperationCanceled, match="command canceled"):
        cancel.run([sys.executable, "-c", "import time; time.sleep(30)"], capture_output=True, timeout=60)
    assert time.monotonic() - t0 < 8                       # three ~1 s stops, not 30-60 s each
    # not canceled: behaves like subprocess.run (input, output, return code)
    with cancel.bound(lambda: False):
        p = cancel.run([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
                       input="hi", capture_output=True, text=True, timeout=30)
    assert p.returncode == 0 and p.stdout.strip() == "HI"
    cancel.sleep(0)                                       # unbound: plain sleep


def test_force_cancel_finalizes_at_once_and_blocks_a_new_run_until_the_worker_stops(plat):  # noqa: F811
    src, tgt = plat.endpoints()
    mid = plat.migration(src, tgt)
    xid = plat.c.post(f"/migrations/{mid}/start", headers=plat.admin, json={"dry_run": False}).json()["id"]
    plat.conductor.tick()
    repo = plat.svc.repo
    assert repo.claim_execution("w-stuck", lease_s=300, caps={"provider:fake"}) == xid
    store = SqlTaskStore(repo, xid)
    t = store.load_tasks()[0]
    t.status = TaskStatus.RUNNING
    store.save_task(t)
    repo.worker_heartbeat("w-stuck", "h", 1, xid)          # a live worker is stuck in this run
    r = plat.c.post(f"/executions/{xid}/cancel", headers=plat.admin).json()
    assert r == {"execution_id": xid, "cancel_requested": True, "force": False}
    assert plat.c.get(f"/executions/{xid}", headers=plat.viewer).json()["status"] == "running"
    r = plat.c.post(f"/migrations/{mid}/cancel?force=true", headers=plat.admin).json()
    assert r["force"] and r["status"] == "canceled"
    x = plat.c.get(f"/executions/{xid}", headers=plat.viewer).json()
    assert x["status"] == "canceled" and "force-canceled by admin" in x["error"] and x["lease_owner"] is None
    tasks = {t.id: t for t in store.load_tasks()}
    assert tasks[t.id].status == TaskStatus.CANCELED and "was running" in tasks[t.id].error
    assert all(t.status != TaskStatus.PENDING for t in tasks.values())
    assert not repo.renew_lease(xid, "w-stuck", 300)        # the worker notices and stops locally
    # the stuck worker is still winding down: no second writer on this migration yet
    busy = plat.c.post(f"/migrations/{mid}/start", headers=plat.admin, json={"dry_run": False})
    assert busy.status_code == 409 and "still stopping" in busy.text
    assert plat.c.post(f"/executions/{xid}/retry", headers=plat.admin).status_code == 409
    repo.worker_heartbeat("w-stuck", "h", 1, None)          # it has stopped
    assert plat.c.post(f"/executions/{xid}/retry", headers=plat.admin).json()["status"] == "queued"
    assert plat.c.post(f"/executions/{xid}/cancel?force=true", headers=plat.admin).json()["force"]
    assert plat.c.post(f"/executions/{xid}/cancel?force=true", headers=plat.admin).status_code == 409
    audit = plat.c.get(f"/audit?object_id={xid}", headers=plat.admin).json()
    assert any("force" in str(a.get("details")) for a in audit)
