"""Purpose: resilience to network cuts (seen live: a cut mid-copy failed the migration and then the
helper cleanup error replaced the real error). Transient-error classification, retry with
cancel-aware backoff, NBD connection loss, a migration that rides out a dropped connection, and
cleanup failures that never mask the outcome.

Usage: pytest tests/test_network_drops.py
"""

from __future__ import annotations

import subprocess
import sys
import threading

import pytest
import requests

from vmigrate.core.errors import ProviderError, SafetyGateError, TransientProviderError, is_transient
from vmigrate.engine.dag import DagRunner, ExecStatus, MemoryTaskStore, TaskSpec
from vmigrate.providers.vhi.nbd import NbdClient, NbdConnectionLost, NbdDiskWriter, agent_command

from .test_platform import plat  # noqa: F401  (fixture)


def test_is_transient_classifies_network_failures_but_not_tls_or_logic_errors():
    assert is_transient(TransientProviderError("x"))
    assert is_transient(ConnectionResetError("reset")) and is_transient(TimeoutError())
    assert is_transient(subprocess.TimeoutExpired("ssh", 30))
    assert is_transient(requests.exceptions.ConnectionError("No route to host"))
    assert is_transient(requests.exceptions.ConnectTimeout("timed out"))
    assert not is_transient(requests.exceptions.SSLError("certificate verify failed"))
    assert not is_transient(ProviderError("disk not found")) and not is_transient(SafetyGateError("gate"))
    assert not is_transient(ValueError("bug"))


def test_is_transient_keystone_errors():
    ksa = pytest.importorskip("keystoneauth1.exceptions.connection")   # installed with the OpenStack extras
    assert is_transient(ksa.ConnectTimeout("Request to https://10.92.2.51:5000/v3/auth/tokens timed out"))
    assert is_transient(ksa.ConnectFailure("No route to host")) and not is_transient(ksa.SSLError("bad cert"))


def test_retry_if_predicate_and_cancel_ends_the_backoff():
    calls = {"n": 0}

    def flaky(ctx):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionResetError("cut")
    slept: list[float] = []
    r = DagRunner(MemoryTaskStore([TaskSpec("f", "flaky", max_attempts=8)]), {"flaky": flaky},
                  retry_if=is_transient, sleep=slept.append)
    assert r.run() == ExecStatus.COMPLETED and calls["n"] == 3 and sum(slept) == 6
    cancel = threading.Event()

    def always(ctx):
        raise ConnectionResetError("still down")

    def sleep(s):
        slept.append(s)
        if len(slept) > 10:
            cancel.set()                                     # operator cancels during a long wait
    slept.clear()
    r = DagRunner(MemoryTaskStore([TaskSpec("a", "always", max_attempts=8)]), {"always": always},
                  retry_if=is_transient, sleep=sleep, cancel_check=cancel.is_set, backoff_max=300)
    assert r.run() in (ExecStatus.CANCELED, ExecStatus.FAILED)
    assert len(slept) <= 12                                  # did not sit out the 2+4+8+16 s waits


def test_nbd_connection_loss_is_transient(tmp_path):
    dev = tmp_path / "dev.img"
    dev.write_bytes(bytes(8 << 20))
    p = subprocess.Popen(agent_command(str(dev), writable=True, python=sys.executable, sudo=False), shell=True,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    w = NbdDiskWriter(NbdClient.over_process(p), 8 << 20, prezeroed=False)
    w.write(0, b"x" * 4096)
    w.flush()
    p.kill()                                                 # the SSH stream dies (network cut)
    p.wait()
    with pytest.raises(NbdConnectionLost) as e:
        for off in range(0, 8 << 20, 1 << 20):
            w.write(off, b"y" * (1 << 20))
        w.flush()
    assert is_transient(e.value)


def test_migration_rides_out_a_dropped_connection(plat):  # noqa: F811
    src, tgt = plat.endpoints(src_extra={"drop_reads": 1})
    mid = plat.migration(src, tgt, engine={"max_parallel_disks": 1})
    xid = plat.c.post(f"/migrations/{mid}/start", headers=plat.admin, json={"dry_run": False}).json()["id"]
    plat.drive(max_ticks=10)
    x = plat.c.get(f"/executions/{xid}", headers=plat.viewer).json()
    assert x["status"] == "completed", x
    tasks = plat.c.get(f"/executions/{xid}/tasks", headers=plat.viewer).json()
    retried = [t for t in tasks if t["kind"] == "replicate_disk" and t["attempts"] == 2]
    assert len(retried) == 1 and len(plat.registry()["vms"]["RIS-Sainath"]["disks"]) == 13


def test_cleanup_failure_never_replaces_the_outcome(plat):  # noqa: F811
    src, tgt = plat.endpoints(tgt_extra={"fail_close": True})
    mid = plat.migration(src, tgt)
    xid = plat.c.post(f"/migrations/{mid}/start", headers=plat.admin, json={"dry_run": False}).json()["id"]
    plat.drive()
    x = plat.c.get(f"/executions/{xid}", headers=plat.viewer).json()
    assert x["status"] == "completed" and not x["error"]                 # the migration itself worked
    ev = plat.svc.repo.get_events(execution_id=xid)
    assert any(e["type"] == "execution.cleanup_failed" and "No route to host" in e["payload"]["error"] for e in ev)
    # a failed run keeps ITS error first; the cleanup problem is appended, not substituted
    from .conftest import fake_vm
    src2 = plat.c.post("/endpoints", headers=plat.admin, json={"name": "ahv2", "type": "fake", "config": {
        "vms": [fake_vm(disks=2)], "fail_read_at": 1 << 20}}).json()["id"]
    tgt2 = plat.c.post("/endpoints", headers=plat.admin, json={"name": "vhi2", "type": "fake", "config": {
        "staging_dir": str(plat.tmp / "tgt2"), "fail_close": True}}).json()["id"]
    r = plat.c.post("/migrations", headers=plat.admin, json={"name": "ris2", "source_endpoint_id": src2,
                                                              "target_endpoint_id": tgt2, "vm": "RIS-Sainath"})
    xid2 = plat.c.post(f"/migrations/{r.json()['id']}/start", headers=plat.admin, json={"dry_run": False}).json()["id"]
    plat.drive()
    x2 = plat.c.get(f"/executions/{xid2}", headers=plat.viewer).json()
    assert x2["status"] == "failed" and x2["error"].startswith("replicate_disk:")
    assert "injected read failure" in x2["error"] and "cleanup (target) incomplete" in x2["error"]
