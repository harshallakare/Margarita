"""Purpose: end-to-end platform tests — REST API -> conductor -> worker -> task DAG -> fake
providers, on a throwaway SQLite DB. Covers auth/RBAC, secret handling, the wizard's disk-count
gate, dry-run safety, a full 13-disk execution, failure + retry (resume), cancel, and worker
crash recovery via lease expiry.

Usage: pytest tests/test_platform.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vmigrate.api.app import create_app
from vmigrate.conductor.service import Conductor
from vmigrate.db.repo import SqlTaskStore
from vmigrate.engine.dag import TaskStatus
from vmigrate.platform.factory import Services
from vmigrate.platform.settings import Settings
from vmigrate.security.auth import hash_password
from vmigrate.security.secretstore import generate_key
from vmigrate.worker.service import Worker

from .conftest import fake_vm

PW = "correct-horse-battery"


@pytest.fixture
def plat(tmp_path):
    st = Settings(database_url=f"sqlite:///{tmp_path}/p.db", secret_key=generate_key(), data_dir=tmp_path / "data",
                  lease_seconds=30, poll_seconds=0.1)
    svc = Services.from_settings(st)
    svc.engine_base.update(chunk_size=1 << 20, checkpoint_every=2 << 20, verify_samples=4,
                           verify_sample_bytes=64 << 10)
    svc.repo.create_user("admin", hash_password(PW), "admin")
    svc.repo.create_user("viewer", hash_password(PW), "viewer")
    client = TestClient(create_app(svc))

    def login(u: str) -> dict[str, str]:
        r = client.post("/auth/token", json={"username": u, "password": PW})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['token']}"}

    class P:
        pass

    p = P()
    p.svc, p.c, p.tmp = svc, client, tmp_path
    p.admin, p.viewer = login("admin"), login("viewer")
    p.conductor, p.worker = Conductor(svc), Worker(svc, "w1")

    def endpoints(src_extra: dict | None = None, tgt_extra: dict | None = None) -> tuple[str, str]:
        s = client.post("/endpoints", headers=p.admin, json={
            "name": "ahv", "type": "fake", "config": {"vms": [fake_vm(disks=13)], **(src_extra or {})},
            "credentials": {"password": "s3cret-src"}}).json()
        t = client.post("/endpoints", headers=p.admin, json={
            "name": "vhi", "type": "fake",
            "config": {"staging_dir": str(tmp_path / "tgt"), "persist": True, **(tgt_extra or {})},
            "credentials": {"password": "s3cret-tgt"}}).json()
        return s["id"], t["id"]

    def migration(src: str, tgt: str, **opts) -> str:
        r = client.post("/migrations", headers=p.admin, json={
            "name": "ris", "source_endpoint_id": src, "target_endpoint_id": tgt, "vm": "RIS-Sainath",
            "options": {"engine": {"max_parallel_disks": 4}, **opts}})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def drive(max_ticks: int = 5) -> None:
        for _ in range(max_ticks):
            p.conductor.tick()
            if p.worker.tick() is None:
                return

    def registry() -> dict:
        f = tmp_path / "tgt" / "registry.json"
        return json.loads(f.read_text()) if f.exists() else {"disks": {}, "vms": {}}

    p.endpoints, p.migration, p.drive, p.registry = endpoints, migration, drive, registry
    return p


def test_auth_and_rbac(plat):
    c = plat.c
    assert c.get("/endpoints").status_code == 401
    assert c.post("/auth/token", json={"username": "admin", "password": "nope"}).status_code == 401
    assert c.get("/auth/me", headers=plat.viewer).json()["role"] == "viewer"
    r = c.post("/endpoints", headers=plat.viewer, json={"name": "x", "type": "fake"})
    assert r.status_code == 403
    assert c.get("/users", headers=plat.viewer).status_code == 403
    assert all("password_hash" not in u for u in c.get("/users", headers=plat.admin).json())
    c.post("/auth/logout", headers=plat.viewer)
    assert c.get("/auth/me", headers=plat.viewer).status_code == 401


def test_credentials_are_encrypted_and_never_returned(plat):
    src, _ = plat.endpoints()
    body = plat.c.get(f"/endpoints/{src}", headers=plat.admin).text
    assert "s3cret-src" not in body and '"credential_keys":["password"]' in body
    raw = Path(plat.svc.settings.database_url.removeprefix("sqlite:///")).read_bytes()
    assert b"s3cret-src" not in raw  # not in the DB file in plaintext either
    r = plat.c.post("/endpoints", headers=plat.admin, json={"name": "bad", "type": "fake",
                                                           "config": {"password": "oops"}})
    assert r.status_code == 400 and "credentials" in r.json()["detail"]
    assert plat.c.post("/endpoints", headers=plat.admin, json={"name": "u", "type": "nope"}).status_code == 400


def test_wizard_inspect_surfaces_true_disk_count_and_gate(plat):
    src, _ = plat.endpoints()
    assert [v["name"] for v in plat.c.get(f"/endpoints/{src}/instances", headers=plat.viewer).json()] == ["RIS-Sainath"]
    r = plat.c.get(f"/endpoints/{src}/instances/RIS-Sainath", headers=plat.viewer).json()
    assert len(r["vm"]["disks"]) == 13 and r["disk_gate"]["ok"] and r["disk_gate"]["legacy_cap_exceeded"]
    # an enumeration path capped at 10 (the Coriolis bug) => gate fails, loudly
    plat.c.patch(f"/endpoints/{src}", headers=plat.admin,
                 json={"config": {"vms": [fake_vm(disks=13)], "secondary_enumeration_cap": 10}})
    g = plat.c.get(f"/endpoints/{src}/instances/RIS-Sainath", headers=plat.viewer).json()["disk_gate"]
    assert not g["ok"] and g["counts"]["fake.secondary"] == 10 and g["counts"]["fake.primary"] == 13
    v = plat.c.post(f"/endpoints/{src}/validate", headers=plat.viewer).json()
    assert v["ok"] and set(v["roles"]) == {"source", "target"}


def test_inspect_failure_returns_real_reason_not_bare_500(plat):
    src, _ = plat.endpoints()
    r = plat.c.get(f"/endpoints/{src}/instances/no-such-vm", headers=plat.viewer)
    assert r.status_code == 502
    d = r.json()["detail"]
    assert "inspecting no-such-vm" in d and "ProviderError" in d and "no VM 'no-such-vm'" in d


def test_dry_run_is_default_and_changes_nothing(plat):
    src, tgt = plat.endpoints()
    mid = plat.migration(src, tgt)
    x = plat.c.post(f"/migrations/{mid}/start", headers=plat.admin).json()
    assert x["dry_run"] is True and x["status"] == "pending"
    plat.drive()
    x = plat.c.get(f"/executions/{x['id']}", headers=plat.viewer).json()
    assert x["status"] == "completed", x
    tasks = plat.c.get(f"/executions/{x['id']}/tasks", headers=plat.viewer).json()
    assert [t["id"] for t in tasks] == ["validate_endpoints", "inspect_instance", "plan"]
    plan = tasks[-1]["result"]["plan"]
    assert sum(1 for a in plan if a["action"] == "target.create_disk") == 13
    assert plat.registry() == {"disks": {}, "vms": {}}  # nothing created on the target
    m = plat.c.get(f"/migrations/{mid}", headers=plat.viewer).json()
    assert m["phase"] == "inspected" and m["disks"] == 13


def test_full_13_disk_execution_through_api_conductor_worker(plat):
    src, tgt = plat.endpoints()
    mid = plat.migration(src, tgt)
    x = plat.c.post(f"/migrations/{mid}/start", headers=plat.admin, json={"dry_run": False}).json()
    assert plat.c.post(f"/migrations/{mid}/start", headers=plat.admin).status_code == 409  # one at a time
    plat.drive()
    xid = x["id"]
    x = plat.c.get(f"/executions/{xid}", headers=plat.viewer).json()
    assert x["status"] == "completed", x
    assert x["task_counts"] == {"completed": 21}  # 8 fixed tasks + 13 replicate_disk tasks
    tasks = plat.c.get(f"/executions/{xid}/tasks", headers=plat.viewer).json()
    disk_tasks = [t for t in tasks if t["kind"] == "replicate_disk"]
    assert len(disk_tasks) == 13 and all(t["progress"] == 100 for t in disk_tasks)
    vm = plat.registry()["vms"]["RIS-Sainath"]
    assert vm["disks"] == [f"vol-RIS-Sainath-disk{i:02d}" for i in range(13)]  # incl. scsi.10-12
    rep = plat.c.get(f"/migrations/{mid}/report", headers=plat.viewer).json()
    assert rep["phase"] == "done" and rep["disk_count_match"] and len(rep["disks"]) == 13
    assert all(d["copied"] and d["committed"] and d["attached"] and d["verify"]["ok"] for d in rep["disks"])
    md = plat.c.get(f"/migrations/{mid}/report?format=md", headers=plat.viewer).text
    assert "# Migration report: ris" in md
    logs = plat.c.get(f"/executions/{xid}/logs", headers=plat.viewer).json()
    assert any("legacy cap" in r["message"] and r["task_id"] == "inspect_instance" for r in logs)
    sse = plat.c.get(f"/events?execution_id={xid}&follow=false", headers=plat.viewer)
    assert sse.headers["content-type"].startswith("text/event-stream")
    assert "event: execution.completed" in sse.text and "event: task.progress" in sse.text
    assert plat.c.post(f"/migrations/{mid}/start", headers=plat.admin, json={"dry_run": False}).status_code == 409


def test_disk_gate_refuses_execution(plat):
    src, tgt = plat.endpoints(src_extra={"secondary_enumeration_cap": 10})
    mid = plat.migration(src, tgt)
    xid = plat.c.post(f"/migrations/{mid}/start", headers=plat.admin, json={"dry_run": False}).json()["id"]
    plat.drive()
    x = plat.c.get(f"/executions/{xid}", headers=plat.viewer).json()
    assert x["status"] == "failed" and "inspect_instance" in x["error"] and "DiskCountMismatch" in x["error"]
    assert plat.registry() == {"disks": {}, "vms": {}}


def test_failure_then_retry_resumes_without_recopying(plat):
    src, tgt = plat.endpoints(tgt_extra={"fail_commit_disk": "RIS-Sainath-disk07"})
    mid = plat.migration(src, tgt)
    xid = plat.c.post(f"/migrations/{mid}/start", headers=plat.admin, json={"dry_run": False}).json()["id"]
    plat.drive()
    x = plat.c.get(f"/executions/{xid}", headers=plat.viewer).json()
    assert x["status"] == "failed" and "disk07" in x["error"]
    store = SqlTaskStore(plat.svc.repo, xid)
    done_before = {t.id for t in store.load_tasks() if t.status == TaskStatus.COMPLETED}
    assert "replicate_disk:RIS-Sainath-d0" in done_before
    # operator fixes the cause, then retries the same execution
    plat.c.patch(f"/endpoints/{tgt}", headers=plat.admin,
                 json={"config": {"staging_dir": str(plat.tmp / "tgt"), "persist": True}})
    assert plat.c.post(f"/executions/{xid}/retry", headers=plat.admin).json()["status"] == "queued"
    plat.drive()
    x = plat.c.get(f"/executions/{xid}", headers=plat.viewer).json()
    assert x["status"] == "completed" and x["runs"] == 2
    assert len(plat.registry()["vms"]["RIS-Sainath"]["disks"]) == 13
    attempts = {t.id: t.attempts for t in store.load_tasks()}
    assert all(attempts[t] == 1 for t in done_before)  # completed tasks were not re-run


def test_retry_after_failure_cleanup_recreates_the_disks(plat):
    """Seen live: osmorphing failed, on_failure=cleanup deleted the copied volumes, then Retry
    resumed at osmorphing and tried to attach a deleted volume (404). After a cleanup the retry
    must start again from disk creation."""
    src, tgt = plat.endpoints(tgt_extra={"fail_commit_disk": "RIS-Sainath-disk07"})
    mid = plat.migration(src, tgt, engine={"max_parallel_disks": 4, "on_failure": "cleanup"})
    xid = plat.c.post(f"/migrations/{mid}/start", headers=plat.admin, json={"dry_run": False}).json()["id"]
    plat.drive()
    x = plat.c.get(f"/executions/{xid}", headers=plat.viewer).json()
    assert x["status"] == "failed" and "disk07" in x["error"]
    assert plat.registry()["disks"] == {}                                    # cleanup removed them
    st = plat.svc.repo.get_migration(mid)["state"]
    assert st["target_cleaned"] and st["created"]["target_disks"] == []
    assert all(d["target"] is None and d["bytes_copied"] == 0 for d in st["disks"].values())
    store = SqlTaskStore(plat.svc.repo, xid)
    assert any(t.status == TaskStatus.FAILED for t in store.load_tasks())  # failed task still shown
    plat.c.patch(f"/endpoints/{tgt}", headers=plat.admin,
                 json={"config": {"staging_dir": str(plat.tmp / "tgt"), "persist": True}})
    assert plat.c.post(f"/executions/{xid}/retry", headers=plat.admin).json()["status"] == "queued"
    plat.drive()
    x = plat.c.get(f"/executions/{xid}", headers=plat.viewer).json()
    assert x["status"] == "completed", x["error"]
    assert len(plat.registry()["vms"]["RIS-Sainath"]["disks"]) == 13
    tasks = store.load_tasks()
    assert len([t for t in tasks if t.kind == "replicate_disk"]) == 13     # children not duplicated
    assert all(t.status == TaskStatus.COMPLETED for t in tasks)
    assert not plat.svc.repo.get_migration(mid)["state"]["target_cleaned"]


def test_cancel_before_a_worker_picks_it_up(plat):
    src, tgt = plat.endpoints()
    mid = plat.migration(src, tgt)
    xid = plat.c.post(f"/migrations/{mid}/start", headers=plat.admin, json={"dry_run": False}).json()["id"]
    plat.conductor.tick()  # -> queued
    assert plat.c.post(f"/migrations/{mid}/cancel", headers=plat.admin).json()["cancel_requested"]
    plat.drive()
    assert plat.c.get(f"/executions/{xid}", headers=plat.viewer).json()["status"] == "canceled"
    assert plat.registry() == {"disks": {}, "vms": {}}


def test_dead_worker_lease_expiry_requeues_and_resumes(plat):
    src, tgt = plat.endpoints()
    mid = plat.migration(src, tgt)
    xid = plat.c.post(f"/migrations/{mid}/start", headers=plat.admin, json={"dry_run": False}).json()["id"]
    plat.conductor.tick()
    repo = plat.svc.repo
    assert repo.claim_execution("crashed-worker", lease_s=-1, caps={"provider:fake"}) == xid  # lease already expired
    store = SqlTaskStore(repo, xid)
    t = store.load_tasks()[0]
    t.status = TaskStatus.RUNNING
    store.save_task(t)
    assert plat.conductor.tick()["requeued"] == 1
    plat.drive()
    x = plat.c.get(f"/executions/{xid}", headers=plat.viewer).json()
    assert x["status"] == "completed" and x["runs"] == 2


def test_endpoint_in_use_cannot_be_deleted_and_workers_listed(plat):
    src, tgt = plat.endpoints()
    mid = plat.migration(src, tgt)
    assert plat.c.delete(f"/endpoints/{src}", headers=plat.admin).status_code == 409
    plat.worker.heartbeat()
    assert plat.c.get("/workers", headers=plat.viewer).json()[0]["alive"]
    assert plat.c.delete(f"/migrations/{mid}", headers=plat.admin).status_code == 204
    assert plat.c.delete(f"/endpoints/{src}", headers=plat.admin).status_code == 204
    assert plat.c.get("/secrets/status", headers=plat.admin).json()["secrets"] == 1


def test_provider_form_fields_and_static_ui(tmp_path):
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html><title>vmigrate</title>")
    (web / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("nope")
    st = Settings(database_url=f"sqlite:///{tmp_path}/p.db", secret_key=generate_key(), data_dir=tmp_path / "d",
                  web_dir=web)
    svc = Services.from_settings(st)
    svc.repo.create_user("a", hash_password(PW), "viewer")
    c = TestClient(create_app(svc))
    h = {"Authorization": "Bearer " + c.post("/auth/token", json={"username": "a", "password": PW}).json()["token"]}
    prov = c.get("/providers", headers=h).json()
    nutanix = next(r for r in prov["sources"] if r["type"] == "nutanix")
    pw = next(f for f in nutanix["fields"] if f["name"] == "password")
    assert pw["secret"] and pw["kind"] == "password"
    assert c.get("/", follow_redirects=False).headers["location"] == "/ui/"
    assert c.get("/ui/assets/app.js").text == "console.log(1)"
    assert "<title>vmigrate</title>" in c.get("/ui/migrations/abc").text      # SPA fallback
    assert "nope" not in c.get("/ui/../secret.txt").text                      # no path traversal
    assert "nope" not in c.get("/ui/%2e%2e/secret.txt").text


def test_execution_transfer_bytes(plat):
    src, tgt = plat.endpoints()
    mid = plat.migration(src, tgt)
    xid = plat.c.post(f"/migrations/{mid}/start", headers=plat.admin, json={"dry_run": False}).json()["id"]
    plat.drive()
    x = plat.c.get(f"/executions/{xid}", headers=plat.admin).json()
    t = x["transfer"]
    assert x["status"] == "completed" and t["disks"] == t["disks_done"] == 13
    assert t["total_bytes"] == t["done_bytes"] == 13 * 9 * (1 << 20)          # fake_vm: 13 x 9 MiB
    listed = plat.c.get("/migrations", headers=plat.admin).json()[0]["last_execution"]["transfer"]
    assert listed == t
    tasks = plat.c.get(f"/executions/{xid}/tasks", headers=plat.admin).json()
    assert all(tk["params"]["size_bytes"] == 9 << 20 for tk in tasks if tk["kind"] == "replicate_disk")


def test_deleted_migration_keeps_runs_and_logs_until_purged(plat):
    c, repo = plat.c, plat.svc.repo
    src, tgt = plat.endpoints()
    mid = plat.migration(src, tgt)
    xid = c.post(f"/migrations/{mid}/start", headers=plat.admin, json={"dry_run": False}).json()["id"]
    plat.drive()
    assert c.get(f"/executions/{xid}", headers=plat.viewer).json()["status"] == "completed"
    assert c.delete(f"/migrations/{mid}", headers=plat.admin).status_code == 204
    # gone from lists and detail pages, actions refused ...
    assert c.get("/migrations", headers=plat.viewer).json() == []
    assert c.get(f"/migrations/{mid}", headers=plat.viewer).status_code == 404
    assert c.post(f"/migrations/{mid}/start", headers=plat.admin, json={}).status_code == 404
    assert c.post(f"/executions/{xid}/retry", headers=plat.admin).status_code == 409
    # ... but the run, its tasks and logs are still there
    gone = c.get("/migrations?deleted=true", headers=plat.viewer).json()
    assert [g["id"] for g in gone] == [mid] and gone[0]["deleted_by"] == "admin"
    assert gone[0]["name"] == f"ris (deleted {mid[:8]})"
    d = c.get(f"/migrations/{mid}?include_deleted=true", headers=plat.viewer).json()
    assert d["deleted_at"]
    assert c.get(f"/executions/{xid}/tasks", headers=plat.viewer).json()
    assert c.get(f"/executions/{xid}/logs", headers=plat.viewer).json()
    rows = c.get(f"/logs?migration_id={mid}&level=INFO", headers=plat.viewer).json()
    assert rows and rows[0]["migration_name"] == f"ris (deleted {mid[:8]})"
    assert c.get("/logs?level=INFO", headers=plat.viewer).json()                  # in the "all" search too
    assert any(x["id"] == xid for x in c.get("/executions", headers=plat.viewer).json())
    # the name is free again, and endpoints are no longer "in use"
    assert plat.migration(src, tgt)
    # retention: purged with its runs after VMIGRATE_DELETED_RETENTION_DAYS
    import time as _t
    assert plat.conductor.purge_deleted(_t.time() + 30 * 86400) == 0               # default 90 days
    plat.conductor._last_purge_deleted = 0
    assert plat.conductor.purge_deleted(_t.time() + 91 * 86400) == 1
    assert repo.get_migration(mid, include_deleted=True) is None and repo.get_execution(xid) is None
    assert repo.get_logs(xid, None, 0, 10) == []


def test_deleting_an_endpoint_purges_deleted_migrations_that_used_it(plat):
    c, repo = plat.c, plat.svc.repo
    src, tgt = plat.endpoints()
    mid = plat.migration(src, tgt)
    assert c.delete(f"/endpoints/{src}", headers=plat.admin).status_code == 409
    assert c.delete(f"/migrations/{mid}", headers=plat.admin).status_code == 204
    assert c.delete(f"/endpoints/{src}", headers=plat.admin).status_code == 204     # FK would otherwise refuse
    assert repo.get_migration(mid, include_deleted=True) is None
