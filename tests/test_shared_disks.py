"""Purpose: cluster shared disks — one migration copies each shared disk, every member attaches it;
members wait without deadlocking; refusals without options.cluster or target multi-attach.

Usage: pytest tests/test_shared_disks.py
"""

from __future__ import annotations

import logging
import threading

import pytest

from vmigrate.config.loader import EngineConfig, MigrationConfig
from vmigrate.core.context import RunContext
from vmigrate.core.errors import SafetyGateError
from vmigrate.engine import MigrationRunner, Phase, StateStore
from vmigrate.engine.shared import MemorySharedDisks
from vmigrate.providers.fake.provider import FakeSource, FakeTarget, PatternDisk

from .test_platform import plat  # noqa: F401  (fixture)


def _node(name: str, shared: list[str]) -> dict:
    return {"name": name, "os": "linux",
            "disks": [{"size": "4MiB", "boot": True}, {"size": "3MiB"}] + [{"size": "2MiB", "shared": s} for s in shared]}


def _runner(tmp, vm, src, tgt, broker, *, cluster="sql", tag=""):
    ctx = RunContext(dry_run=False, run_id=f"r-{vm}{tag}", logger=logging.getLogger("t"))
    src.ctx = tgt.ctx = ctx
    e = EngineConfig(state_dir=tmp / f"state-{vm}{tag}", report_dir=tmp / "reports", chunk_size=1 << 20,
                     checkpoint_every=1 << 20, verify_samples=4, verify_sample_bytes=64 << 10, max_parallel_disks=2)
    job = MigrationConfig(name=f"m-{vm}{tag}", source="s", target="t", vm=vm, cluster=cluster)
    r = MigrationRunner(job, e, src, tgt, ctx, StateStore(e.state_dir), shared=broker, migration_id=f"m-{vm}")
    r.shared_poll = 0.05
    return r


def test_two_node_cluster_copies_shared_disks_once(tmp_path):
    vms = [_node("node1", ["q", "data"]), _node("node2", ["q", "data"])]
    broker = MemorySharedDisks()
    tgt_cfg = {"type": "fake", "staging_dir": str(tmp_path / "tgt")}
    tgt1 = FakeTarget("t", tgt_cfg, RunContext(dry_run=False))
    tgt2 = FakeTarget("t", tgt_cfg, RunContext(dry_run=False))
    tgt2.disks, tgt2.vms, tgt2.calls = tgt1.disks, tgt1.vms, tgt1.calls          # one shared target
    r1 = _runner(tmp_path, "node1", FakeSource("s", {"vms": vms}, RunContext(dry_run=False)), tgt1, broker)
    r2 = _runner(tmp_path, "node2", FakeSource("s", {"vms": vms}, RunContext(dry_run=False)), tgt2, broker)
    out: dict[str, object] = {}

    def go(name, r):
        try:
            out[name] = r.run().phase
        except Exception as e:  # noqa: BLE001
            out[name] = e
    ts = [threading.Thread(target=go, args=(n, r)) for n, r in (("node1", r1), ("node2", r2))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=60)
    assert out == {"node1": Phase.DONE, "node2": Phase.DONE}, out
    shared_creates = [c for c in tgt1.calls if c.startswith("create_disk:sql-shared-")]
    assert sorted(shared_creates) == ["create_disk:sql-shared-data", "create_disk:sql-shared-q"]   # once each
    for vm in ("node1", "node2"):
        disks = tgt1.vms[vm]["disks"]
        assert len(disks) == 4 and "vol-sql-shared-q" in disks and "vol-sql-shared-data" in disks
    ref = tgt1.disks["sql-shared-data"]
    assert tgt1._final(ref).read_bytes() == PatternDisk("shared:data", 2 << 20).read(0, 2 << 20)
    owners = {broker.get("sql", s)["owner"] for s in ("q", "data")}
    assert owners <= {"m-node1", "m-node2"} and all(broker.get("sql", s)["status"] == "done" for s in ("q", "data"))
    for r in (r1, r2):          # members never list the shared disk as created by them (failure cleanup safe)
        mine = {d["name"] for d in r.state.created["target_disks"]}
        member_of = [d for d in r.state.disks.values() if d.shared_owner and d.shared_owner != r.migration_id]
        assert all(d.target["name"] not in mine for d in member_of)


def test_refusals(tmp_path):
    vms = [_node("node1", ["q"])]
    src = FakeSource("s", {"vms": vms}, RunContext(dry_run=False))
    tgt = FakeTarget("t", {"type": "fake", "staging_dir": str(tmp_path / "t")}, RunContext(dry_run=False))
    with pytest.raises(SafetyGateError, match="options.cluster"):
        _runner(tmp_path, "node1", src, tgt, MemorySharedDisks(), cluster=None).run()
    tgt2 = FakeTarget("t", {"type": "fake", "staging_dir": str(tmp_path / "t2"), "no_shared": True}, RunContext(dry_run=False))
    with pytest.raises(SafetyGateError, match="shared disks disabled"):
        _runner(tmp_path, "node1", src, tgt2, MemorySharedDisks(), tag="b").run()


def test_platform_cluster_with_db_broker(plat):  # noqa: F811
    c, h = plat.c, plat.admin
    src = c.post("/endpoints", headers=h, json={"name": "s", "type": "fake",
                                                "config": {"vms": [_node("node1", ["q"]), _node("node2", ["q"])]}}).json()
    tgt = c.post("/endpoints", headers=h, json={"name": "t", "type": "fake",
                                                "config": {"staging_dir": str(plat.tmp / "tgt"), "persist": True}}).json()
    ids = []
    for vm in ("node1", "node2"):
        r = c.post("/migrations", headers=h, json={"name": f"m-{vm}", "source_endpoint_id": src["id"],
                                                    "target_endpoint_id": tgt["id"], "vm": vm,
                                                    "options": {"cluster": "sql"}})
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    for mid in ids:
        xid = c.post(f"/migrations/{mid}/start", headers=h, json={"dry_run": False}).json()["id"]
        plat.drive()
        assert c.get(f"/executions/{xid}", headers=h).json()["status"] == "completed"
    reg = plat.registry()
    assert reg["vms"]["node1"]["disks"][2] == reg["vms"]["node2"]["disks"][2] == "vol-sql-shared-q"
    from vmigrate.db.repo import DbSharedDisks
    rows = DbSharedDisks(plat.svc.repo).list("sql")
    assert [(r_["shared_id"], r_["owner"], r_["status"]) for r_ in rows] == [("q", ids[0], "done")]
