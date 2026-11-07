"""Purpose: cross-site transfers — the export agent at the source site serves provider calls and
snapshot disks; a target-site worker migrates through RemoteSource. The agent runs in-process
(FastAPI TestClient), everything else is the real platform.

Usage: pytest tests/test_export.py
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from vmigrate.conductor.service import Conductor
from vmigrate.core.errors import ConfigError, DryRunViolation, ProviderError
from vmigrate.core.interfaces import CheckpointRef, SnapshotRef, VmRef
from vmigrate.core.ranges import ByteRange
from vmigrate.export.agent import cert_fingerprint, ensure_self_signed, make_agent_app
from vmigrate.export.codec import decode, derive_key, encode, sign, verify
from vmigrate.export.remote import AgentClient
from vmigrate.providers.fake.provider import PatternDisk
from vmigrate.worker.service import Worker

from .conftest import fake_vm
from .test_platform import plat  # noqa: F401  (fixture)
from .test_pure_logic import spec


def test_codec_roundtrip_and_refusals():
    s = spec()
    snap = SnapshotRef("s1", VmRef("v", "vm"), True, {"disks": {"a": [1, 2]}})
    obj = [snap, CheckpointRef("c", {"t": 1}), s, s.disks[0], ByteRange(5, 7), {"k": None}, "x", 3]
    back = decode(encode(obj))
    assert back[0] == snap and back[1] == CheckpointRef("c", {"t": 1}) and back[2].to_dict() == s.to_dict()
    assert back[3] == s.disks[0] and back[4] == ByteRange(5, 7) and back[5:] == [{"k": None}, "x", 3]
    with pytest.raises(TypeError):
        encode(object())
    with pytest.raises(TypeError):
        decode({"__t": "os.system", "v": "rm"})


def test_signatures():
    k = derive_key("secret")
    h = sign(k, "POST", "/v1/call", b"{}", now=1000)
    assert verify(k, "POST", "/v1/call", b"{}", h, now=1100)
    assert not verify(k, "POST", "/v1/call", b"{ }", h, now=1100)         # body tampered
    assert not verify(k, "GET", "/v1/call", b"{}", h, now=1100)           # method
    assert not verify(derive_key("other"), "POST", "/v1/call", b"{}", h, now=1100)
    assert not verify(k, "POST", "/v1/call", b"{}", h, now=2000)          # replay window
    assert not verify(k, "POST", "/v1/call", b"{}", None)


def test_self_signed_cert_and_plain_http_refused(tmp_path):
    ensure_self_signed(tmp_path / "c.pem", tmp_path / "k.pem", "agent.site-a")
    fp = cert_fingerprint(tmp_path / "c.pem")
    assert len(fp) == 64 and (tmp_path / "k.pem").stat().st_mode & 0o777 == 0o600
    ensure_self_signed(tmp_path / "c.pem", tmp_path / "k.pem", "agent.site-a")   # idempotent
    assert cert_fingerprint(tmp_path / "c.pem") == fp
    with pytest.raises(ConfigError, match="plain http"):
        AgentClient("http://agent:8443", b"k")


def _sites(plat, *, disks=5):  # noqa: F811
    c, h = plat.c, plat.admin
    src = c.post("/endpoints", headers=h, json={"name": "ahv-pune", "type": "fake", "region": "pune",
                                                 "config": {"vms": [fake_vm(disks=disks)]}}).json()
    tgt = c.post("/endpoints", headers=h, json={"name": "vhi-dc2", "type": "fake", "region": "dc2",
                                                 "config": {"staging_dir": str(plat.tmp / "tgt"), "persist": True}}).json()
    key = derive_key(plat.svc.settings.secret_key)
    app = make_agent_app(plat.svc, key)
    agent_http = TestClient(app, base_url="https://agent.pune")
    orig = plat.svc.remote_source
    plat.svc.remote_source = lambda ep, ctx, http=None: orig(ep, ctx, http=agent_http)
    return src, tgt, app, agent_http


def _register_agent(plat, region="pune"):  # noqa: F811
    plat.svc.repo.worker_heartbeat("export-agent-pune", "agent", 1, None, region=region,
                                   capabilities=["export-agent", "provider:fake"], export_url="https://agent.pune")


def test_cross_site_migration_through_export_agent(plat):  # noqa: F811
    src, tgt, app, agent_http = _sites(plat)
    _register_agent(plat)
    mid = plat.c.post("/migrations", headers=plat.admin, json={
        "name": "x", "source_endpoint_id": src["id"], "target_endpoint_id": tgt["id"], "vm": "RIS-Sainath"}).json()["id"]
    xid = plat.c.post(f"/migrations/{mid}/start", headers=plat.admin, json={"dry_run": False}).json()["id"]
    cond = Conductor(plat.svc)
    # the target-site worker has no source capability at all: cross-site work only needs the target side
    wb = Worker(plat.svc, "w-dc2", region="dc2", capabilities={"provider:fake"})
    wb.heartbeat()
    cond.tick()
    x = plat.c.get(f"/executions/{xid}", headers=plat.admin).json()
    assert x["region"] == "dc2" and x["waiting_reason"] is None     # the agent row never counts as a worker
    assert wb.tick() == xid
    x = plat.c.get(f"/executions/{xid}", headers=plat.admin).json()
    assert x["status"] == "completed", x
    reg = plat.registry()
    assert len(reg["vms"]["RIS-Sainath"]["disks"]) == 5
    for i in range(5):
        p = plat.tmp / "tgt" / "volumes" / f"RIS-Sainath-disk{i:02d}.img"
        size = p.stat().st_size
        assert p.read_bytes() == PatternDisk(f"RIS-Sainath:{i}", size).read(0, size)   # bytes crossed the agent
    assert app.state.agent._sessions == {} and app.state.agent._readers == {}       # closed at the end


def test_cross_site_without_agent_fails_clearly_and_dry_run_is_enforced_remotely(plat):  # noqa: F811
    src, tgt, app, agent_http = _sites(plat, disks=2)
    mid = plat.c.post("/migrations", headers=plat.admin, json={
        "name": "y", "source_endpoint_id": src["id"], "target_endpoint_id": tgt["id"], "vm": "RIS-Sainath"}).json()["id"]
    xid = plat.c.post(f"/migrations/{mid}/start", headers=plat.admin, json={"dry_run": True}).json()["id"]
    cond, wb = Conductor(plat.svc), Worker(plat.svc, "w-dc2", region="dc2", capabilities={"provider:fake"})
    wb.heartbeat()
    cond.tick()
    wb.tick()
    x = plat.c.get(f"/executions/{xid}", headers=plat.admin).json()
    assert x["status"] == "failed" and "no live export agent in region 'pune'" in x["error"]

    _register_agent(plat)
    from vmigrate.core.context import RunContext
    ep = plat.svc.repo.get_endpoint(src["id"])
    rs = plat.svc.build_source(ep, RunContext(dry_run=True, run_id="r1"), target_ep=plat.svc.repo.get_endpoint(tgt["id"]))
    vm = rs.find_vm("RIS-Sainath")
    assert rs.inspect_vm(vm).name == "RIS-Sainath" and len(rs.inspect_vm(vm).disks) == 2
    with pytest.raises(DryRunViolation):
        rs.create_snapshot(vm, False)                         # refused locally before any call
    with pytest.raises(DryRunViolation):
        rs._call("create_snapshot", vm, False)                # and by the agent's own provider
    with pytest.raises(ProviderError, match="no VM"):
        rs.find_vm("nope")                                    # provider errors keep their type
    rs.close()


def test_agent_rejects_unsigned_and_unexported_calls(plat):  # noqa: F811
    src, tgt, app, agent_http = _sites(plat, disks=1)
    assert agent_http.get("/v1/health").json()["ok"]
    r = agent_http.post("/v1/call", json={"endpoint_id": src["id"], "run_id": "r", "method": "find_vm"})
    assert r.status_code == 401
    c = AgentClient("https://agent.pune", derive_key(plat.svc.settings.secret_key), http=agent_http)
    with pytest.raises(Exception, match="not exported"):
        c.request("POST", "/v1/call", body={"endpoint_id": src["id"], "run_id": "r", "method": "open_disk_writer"})
    bad = AgentClient("https://agent.pune", derive_key("wrong"), http=agent_http)
    with pytest.raises(Exception, match="signature"):
        bad.request("POST", "/v1/call", body={"endpoint_id": src["id"], "run_id": "r", "method": "list_vms"})


def test_expire_idle_sessions(plat):  # noqa: F811
    src, tgt, app, agent_http = _sites(plat, disks=1)
    ag = app.state.agent
    ag.session(src["id"], "run-1", True)
    assert ag.expire(now=time.time() + 10) == 0
    assert ag.expire(now=time.time() + 5 * 3600) == 1 and ag._sessions == {}
