"""Purpose: replica mode end to end — API -> conductor -> worker -> replica task graphs on the fake
pair: initial full sync, incremental CBT and compare syncs (byte-exact), restore points + retention,
deploy (latest + restore point) while the replica keeps syncing, safety refusals, resume of an
interrupted sync, schedules (cron), delete-disks. Plus pure cron + compare-copy tests.

Usage: pytest tests/test_replica.py
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from vmigrate.core.cron import Cron, CronError
from vmigrate.core.fileio import RawFileReader, RawFileWriter
from vmigrate.engine.copier import copy_disk
from vmigrate.providers.fake.provider import PatternDisk

from .test_platform import plat  # noqa: F401  (fixture)

SIZE = 9 << 20


def _vm(disks: int = 3) -> dict:
    return {"name": "RIS-Sainath", "os": "linux", "disks": [{"size": "9MiB", "boot": i == 0} for i in range(disks)]}


@pytest.fixture
def rep(plat):  # noqa: F811
    c, h = plat.c, plat.admin
    src = c.post("/endpoints", headers=h, json={"name": "ahv", "type": "fake",
                                                 "config": {"vms": [_vm()], "cbt": True, "generation": 0}}).json()["id"]
    tgt = c.post("/endpoints", headers=h, json={"name": "vhi", "type": "fake",
                                                 "config": {"staging_dir": str(plat.tmp / "tgt"), "persist": True}}).json()["id"]
    r = c.post("/replicas", headers=h, json={"name": "ris-dr", "source_endpoint_id": src, "target_endpoint_id": tgt,
                                              "vm": "RIS-Sainath", "options": {"retention": 2}})
    assert r.status_code == 201, r.text

    def set_source(**cfg):
        c.patch(f"/endpoints/{src}", headers=h, json={"config": {"vms": [_vm()], "cbt": True, "generation": 0, **cfg}})

    def run(path: str, body: dict | None = None) -> dict:
        x = c.post(f"/replicas/{r.json()['id']}/{path}", headers=h, json=body or {"dry_run": False})
        assert x.status_code == 202, x.text
        plat.drive()
        return c.get(f"/executions/{x.json()['id']}", headers=h).json()

    def get() -> dict:
        return c.get(f"/replicas/{r.json()['id']}", headers=h).json()

    def replica_bytes(i: int) -> bytes:
        return (plat.tmp / "tgt" / "volumes" / f"ris-dr-r{i:02d}.img").read_bytes()

    plat.rid, plat.set_source, plat.run, plat.get, plat.replica_bytes = r.json()["id"], set_source, run, get, replica_bytes
    return plat


def _expected(i: int, gen: int) -> bytes:
    return PatternDisk(f"RIS-Sainath:{i}", SIZE, generation=gen).read(0, SIZE)


def test_sync_modes_are_byte_exact_and_incremental(rep):
    x = rep.run("execute", {"dry_run": True})
    assert x["status"] == "completed" and x["kind"] == "replica_sync"
    assert rep.registry() == {"disks": {}, "vms": {}}                      # dry-run created nothing
    assert rep.run("execute")["status"] == "completed"
    s = rep.get()
    assert s["consistent"] and s["last_sync"]["modes"] == ["full"] and s["disks"] == 3
    assert all(rep.replica_bytes(i) == _expected(i, 0) for i in range(3))

    rep.set_source(generation=1)                                           # ~1/8 of blocks change
    assert rep.run("execute")["status"] == "completed"
    last = rep.get()["last_sync"]
    assert last["modes"] == ["cbt"] and last["bytes_read"] < 3 * SIZE // 3   # only changed regions read
    assert all(rep.replica_bytes(i) == _expected(i, 1) for i in range(3))

    rep.set_source(generation=2, cbt=False)                                # no change tracking
    assert rep.run("execute")["status"] == "completed"
    last = rep.get()["last_sync"]
    assert last["modes"] == ["compare"] and last["bytes_read"] == 3 * SIZE and last["bytes_written"] < SIZE
    assert all(rep.replica_bytes(i) == _expected(i, 2) for i in range(3))
    assert [p["id"] for p in rep.get()["restore_points"]] == [last["id"], rep.get()["syncs"][1]["id"]]  # retention 2


def test_deploy_latest_and_restore_point_while_replica_keeps_syncing(rep):
    rep.run("execute")
    rep.set_source(generation=1)
    rep.run("execute")
    rps = rep.get()["restore_points"]
    oldest = rps[-1]["id"]                                                  # generation 0
    x = rep.run("deploy", {"dry_run": True, "deploy_name": "ris-test"})
    assert x["status"] == "completed" and "ris-test" not in rep.registry()["vms"]
    x = rep.run("deploy", {"dry_run": False, "deploy_name": "ris-test", "restore_point": oldest})
    assert x["status"] == "completed", x
    vm = rep.registry()["vms"]["ris-test"]
    assert len(vm["disks"]) == 3
    assert (rep.tmp / "tgt" / "volumes" / "ris-test-disk00.img").read_bytes() == _expected(0, 0)
    assert rep.run("deploy", {"dry_run": False, "deploy_name": "ris-latest"})["status"] == "completed"
    assert (rep.tmp / "tgt" / "volumes" / "ris-latest-disk02.img").read_bytes() == _expected(2, 1)
    deps = rep.get()["deployments"]
    assert [d["status"] for d in deps] == ["done", "done"]
    rep.set_source(generation=2)                                            # replica keeps working
    assert rep.run("execute")["status"] == "completed"
    assert rep.replica_bytes(0) == _expected(0, 2)
    assert (rep.tmp / "tgt" / "volumes" / "ris-latest-disk00.img").read_bytes() == _expected(0, 1)  # deployed VM untouched


def test_deploy_refusals(rep):
    r = rep.c.post(f"/replicas/{rep.rid}/deploy", headers=rep.admin, json={"dry_run": False})
    assert r.status_code == 409 and "no completed sync" in r.json()["detail"]
    rep.run("execute")
    r = rep.c.post(f"/replicas/{rep.rid}/deploy", headers=rep.admin, json={"restore_point": "nope"})
    assert r.status_code == 404
    assert rep.c.post(f"/migrations/{rep.rid}/start", headers=rep.admin).status_code == 409


def test_interrupted_sync_resumes_with_same_snapshot_and_blocks_latest_deploy(rep):
    rep.run("execute")
    rep.set_source(generation=1, fail_read_at=4 << 20)                     # source read dies mid-disk
    x = rep.run("execute")
    assert x["status"] == "failed"
    s = rep.get()
    assert not s["consistent"] and s["current_sync"] is not None
    r = rep.c.post(f"/replicas/{rep.rid}/deploy", headers=rep.admin, json={"dry_run": False})
    assert r.status_code == 409 and "mid-sync" in r.json()["detail"]
    sync_id = s["current_sync"]["id"]
    rep.set_source(generation=5)        # source moved on, but the resumed sync must use ITS snapshot (gen 1)
    assert rep.c.post(f"/executions/{x['id']}/retry", headers=rep.admin).status_code == 200
    rep.drive()
    s = rep.get()
    assert s["consistent"] and s["last_sync"]["id"] == sync_id
    assert all(rep.replica_bytes(i) == _expected(i, 1) for i in range(3))


def test_disk_set_change_is_refused(rep):
    rep.run("execute")
    rep.c.patch(f"/endpoints/{rep.svc.repo.get_migration(rep.rid)['source_endpoint_id']}", headers=rep.admin,
                json={"config": {"vms": [_vm(disks=4)], "cbt": True}})
    x = rep.run("execute")
    assert x["status"] == "failed" and "disk set changed" in x["error"]


def test_schedules_fire_via_conductor_and_skip_when_busy(rep):
    bad = rep.c.post(f"/replicas/{rep.rid}/schedules", headers=rep.admin, json={"cron": "61 * * * *"})
    assert bad.status_code == 400
    sc = rep.c.post(f"/replicas/{rep.rid}/schedules", headers=rep.admin, json={"cron": "*/5 * * * *"}).json()
    assert len(sc["upcoming"]) == 3
    assert rep.c.get("/schedules/preview?cron=@daily", headers=rep.viewer).json()["valid"]
    repo = rep.svc.repo
    repo.update_schedule(sc["id"], next_run_at=1.0)                         # make it due
    assert rep.conductor.tick()["scheduled"] == 1
    rep.drive()
    s = repo.get_schedule(sc["id"])
    assert s["next_run_at"] > 1.0 and s["last_execution_id"]
    assert repo.get_execution(s["last_execution_id"])["status"] == "completed"
    assert rep.get()["last_sync"]["modes"] == ["full"]
    # due again while a manual sync is queued -> skipped, not stacked
    rep.c.post(f"/replicas/{rep.rid}/execute", headers=rep.admin, json={"dry_run": False})
    repo.update_schedule(sc["id"], next_run_at=1.0)
    assert rep.conductor.tick()["scheduled"] == 0
    assert "skipped" in repo.get_schedule(sc["id"])["last_skip_reason"]
    rep.c.patch(f"/schedules/{sc['id']}", headers=rep.admin, json={"enabled": False})
    assert rep.c.get(f"/replicas/{rep.rid}", headers=rep.viewer).json()["schedules"][0]["enabled"] is False


def test_delete_disks_then_delete_replica(rep):
    rep.run("execute")
    assert rep.c.delete(f"/replicas/{rep.rid}", headers=rep.admin).status_code == 409   # disks would be orphaned
    x = rep.run("delete-disks", {"dry_run": True})
    plan = rep.c.get(f"/executions/{x['id']}/tasks", headers=rep.admin).json()[-1]["result"]["plan"]
    assert sum(1 for a in plan if a["action"] == "target.delete_disk") == 3
    assert rep.run("delete-disks")["status"] == "completed"
    assert not [k for k in rep.registry()["disks"] if k.startswith("ris-dr-r")]
    assert rep.c.delete(f"/replicas/{rep.rid}", headers=rep.admin).status_code == 204


def test_schema_upgrade_adds_columns(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from vmigrate.db.schema import upgrade_schema
    eng = create_engine(f"sqlite:///{tmp_path}/old.db")
    with eng.begin() as c:   # an executions table from before the params column existed
        c.execute(text("CREATE TABLE executions (id VARCHAR(32) PRIMARY KEY, migration_id VARCHAR(32))"))
    assert "executions.params" in upgrade_schema(eng)
    assert "params" in {c["name"] for c in inspect(eng).get_columns("executions")}
    assert upgrade_schema(eng) == []


# ---------------------------------------------------------------------- pure

def test_cron():
    t0 = datetime(2026, 11, 7, 12, 7, 30, tzinfo=timezone.utc).timestamp()   # Saturday

    def nxt(e: str, n: int = 2) -> list[str]:
        return [datetime.fromtimestamp(x, timezone.utc).strftime("%a %d %H:%M") for x in Cron.parse(e).upcoming(t0, n)]

    assert nxt("*/15 * * * *") == ["Sat 07 12:15", "Sat 07 12:30"]
    assert nxt("30 1 * * MON-FRI") == ["Mon 09 01:30", "Tue 10 01:30"]
    assert nxt("0 0 * * 7") == ["Sun 08 00:00", "Sun 15 00:00"]
    assert nxt("@hourly") == ["Sat 07 13:00", "Sat 07 14:00"]
    assert nxt("0 9 1 * *", 1) == ["Tue 01 09:00"]
    for bad in ("* * * *", "60 * * * *", "*/0 * * * *", "0 0 31 2 *", "a b c d e"):
        with pytest.raises(CronError):
            Cron.parse(bad).next_after(t0)


def test_compare_copy_writes_only_differing_blocks(tmp_path):
    old = PatternDisk("x", SIZE, generation=0)
    new = PatternDisk("x", SIZE, generation=1)
    p = tmp_path / "replica.raw"
    with RawFileWriter(p, SIZE) as w:
        w.write(0, old.read(0, SIZE))
    w = RawFileWriter(p, SIZE)
    w.prezeroed = False
    with w, RawFileReader(p) as cmp:
        st = copy_disk(new, w, chunk_size=1 << 20, compare=cmp)
    assert p.read_bytes() == new.read(0, SIZE)
    assert st.bytes_written == len(old.changed_blocks(0, 1)) * (64 << 10)
    assert st.bytes_written + st.bytes_unchanged == SIZE


def test_cutover_final_sync_then_deploy(rep):
    r = rep.c.post(f"/replicas/{rep.rid}/cutover", headers=rep.admin, json={"dry_run": False})
    assert r.status_code == 409 and "initial sync" in r.text                # nothing synced yet
    rep.run("execute")                                                      # initial sync (generation 0)
    rep.set_source(generation=1)                                            # source keeps changing
    x = rep.run("cutover", {"dry_run": True, "shutdown_source": True})
    assert x["status"] == "completed" and rep.registry()["vms"] == {}       # plan only
    x = rep.run("cutover", {"dry_run": False, "shutdown_source": True, "deploy_name": "ris-prod"})
    assert x["status"] == "completed", x
    s = rep.get()
    assert s["last_sync"]["modes"] == ["cbt"]                               # the final sync was a delta
    assert (rep.tmp / "tgt" / "volumes" / "ris-prod-disk01.img").read_bytes() == _expected(1, 1)  # latest data
    c = s["cutovers"][0]
    assert c["status"] == "done" and c["shutdown_source"] and "powered off" in c["source"]
    assert c["target_vm"] and s["deployments"][0]["status"] == "done"


def test_cutover_without_shutdown_and_shutdown_failure(rep):
    rep.run("execute")
    x = rep.run("cutover", {"dry_run": False, "deploy_name": "ris-a"})
    assert x["status"] == "completed"
    assert rep.get()["cutovers"][0]["source"] == "left running"
    rep.set_source(fail_shutdown="guest did not respond")
    x = rep.run("cutover", {"dry_run": False, "shutdown_source": True, "deploy_name": "ris-b"})
    assert x["status"] == "failed" and "guest did not respond" in x["error"]
    c = rep.get()["cutovers"][0]
    assert c["status"] == "failed" and "ris-b" not in rep.registry()["vms"]   # nothing deployed after a failed stop
