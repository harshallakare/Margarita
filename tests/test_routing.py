"""Purpose: worker routing — capability detection, requirements from endpoints (regions, required tags,
inherent provider needs), claim filtering, and the conductor's "waiting for a suitable worker" reason.

Usage: pytest tests/test_routing.py
"""

from __future__ import annotations

from vmigrate.conductor.service import Conductor
from vmigrate.core.routing import can_run, detect_capabilities, requirements, waiting_reason
from vmigrate.worker.service import Worker

from .conftest import fake_vm
from .test_platform import plat  # noqa: F401  (fixture)


def test_detect_capabilities():
    caps = detect_capabilities({"VMIGRATE_WORKER_TAGS": "site-a, gpu"}, which=lambda t: t in ("qemu-img", "iscsiadm"),
                               has_module=lambda m: m == "boto3", is_dir=lambda p: p == "/mnt/vstorage", euid=0)
    assert {"provider:aws", "provider:fake", "tool:qemu-img", "tool:iscsiadm", "vstorage", "root", "site-a", "gpu"} <= caps
    assert "provider:vmware" not in caps and "tool:nbdkit" not in caps


def test_requirements_and_matching():
    src = {"type": "nutanix", "region": "pune", "required_tags": ["tool:iscsiadm"]}
    tgt = {"type": "vhi", "region": "pune", "required_tags": ["vstorage"]}
    r = requirements(src, tgt)
    assert r.region == "pune" and not r.cross_site
    assert r.caps == {"provider:nutanix", "provider:vhi", "tool:qemu-img", "root", "tool:iscsiadm", "vstorage"}
    good = r.caps | {"extra"}
    assert can_run("pune", good, r.to_dict()) == (True, "ok")
    assert not can_run("mumbai", good, r.to_dict())[0]
    ok, why = can_run("pune", good - {"root"}, r.to_dict())
    assert not ok and "root" in why
    cross = requirements({**src, "region": "pune"}, {**tgt, "region": "dc2"})
    assert cross.cross_site and cross.region == "dc2" and cross.source_region == "pune"
    assert "provider:nutanix" not in cross.caps and "tool:iscsiadm" not in cross.caps   # served by the source site
    assert can_run(None, None, None)[0]                                                  # legacy executions


def test_waiting_reason():
    req = {"region": "b", "caps": ["provider:fake"]}
    assert waiting_reason([], req, 0).startswith("no live worker")
    w = [{"id": "wa", "region": "a", "capabilities": ["provider:fake"], "heartbeat_at": 10}]
    assert "needs region 'b'" in waiting_reason(w, req, 0)
    assert waiting_reason(w + [{"id": "wb", "region": "b", "capabilities": ["provider:fake"], "heartbeat_at": 10}],
                          req, 0) is None


def test_jobs_go_only_to_capable_workers(plat):  # noqa: F811
    c, h = plat.c, plat.admin
    src = c.post("/endpoints", headers=h, json={"name": "s", "type": "fake", "config": {"vms": [fake_vm(disks=2)]},
                                                "region": "site-b"}).json()
    tgt = c.post("/endpoints", headers=h, json={"name": "t", "type": "fake", "region": "site-b",
                                                "required_tags": ["vstorage"],
                                                "config": {"staging_dir": str(plat.tmp / "t"), "persist": True}}).json()
    assert tgt["region"] == "site-b" and tgt["required_tags"] == ["vstorage"]
    mid = c.post("/migrations", headers=h, json={"name": "m", "source_endpoint_id": src["id"],
                                                  "target_endpoint_id": tgt["id"], "vm": "RIS-Sainath"}).json()["id"]
    xid = c.post(f"/migrations/{mid}/start", headers=h).json()["id"]
    cond = Conductor(plat.svc)
    wa = Worker(plat.svc, "w-site-a", region="site-a", capabilities={"provider:fake", "vstorage"})
    wb_untagged = Worker(plat.svc, "w-site-b-plain", region="site-b", capabilities={"provider:fake"})
    for w in (wa, wb_untagged):
        w.heartbeat()
    cond.tick()
    x = c.get(f"/executions/{xid}", headers=h).json()
    assert x["status"] == "queued" and x["region"] == "site-b"
    assert "needs region 'site-b'" in x["waiting_reason"] and "missing capabilities ['vstorage']" in x["waiting_reason"]
    assert wa.tick() is None and wb_untagged.tick() is None     # neither may take it
    wb = Worker(plat.svc, "w-site-b-vhi", region="site-b", capabilities={"provider:fake", "vstorage"})
    wb.heartbeat()
    cond.tick()
    assert c.get(f"/executions/{xid}", headers=h).json()["waiting_reason"] is None
    assert wb.tick() == xid
    assert c.get(f"/executions/{xid}", headers=h).json()["status"] == "completed"
    workers = {w["id"]: w for w in c.get("/workers", headers=h).json()}
    assert workers["w-site-b-vhi"]["region"] == "site-b" and "vstorage" in workers["w-site-b-vhi"]["capabilities"]


def test_target_resources_for_wizard_dropdowns(plat):  # noqa: F811
    c, h = plat.c, plat.admin
    tgt = c.post("/endpoints", headers=h, json={"name": "vhi", "type": "fake",
                                                "config": {"staging_dir": str(plat.tmp / "x"), "networks": ["a", "b"]}}).json()
    r = c.get(f"/endpoints/{tgt['id']}/resources", headers=plat.viewer).json()
    assert [n["name"] for n in r["networks"]] == ["a", "b"] and r["storage"] and r["flavors"][0]["vcpus"] == 2
    assert r["errors"] == {}


def test_vhi_target_lists_resources(tmp_path):
    import logging

    from vmigrate.core.context import RunContext
    from vmigrate.providers.vhi.target import VhiTarget

    from .fakes_platform import FakeOpenStack, FakeQemu
    t = VhiTarget("vhi", {"type": "vhi", "auth_url": "x", "username": "u", "project_id": "proj-1",
                          "vstorage_mount": str(tmp_path), "staging_dir": str(tmp_path / "s"), "volume_type": "vmds"},
                  RunContext(dry_run=True, logger=logging.getLogger("t")), api=FakeOpenStack(tmp_path), qemu=FakeQemu())
    assert [n["name"] for n in t.list_networks()] == ["backup", "prod-net"]
    assert [v["name"] for v in t.list_storage()] == ["vmds", "vmds-hdd"]
    assert [f["vcpus"] for f in t.list_flavors()] == [8, 16]          # smallest first
    assert [i["name"] for i in t.list_images()] == ["Cirros", "ubuntu-24.04"]   # active only, by name


def test_discover_lists_for_the_endpoint_form(plat):  # noqa: F811
    c, h = plat.c, plat.admin
    cfg = {"staging_dir": str(plat.tmp / "x"), "networks": ["dmz", "lan"], "images_need_password": True}
    # unsaved settings, as typed in the form
    r = c.post("/endpoints/discover", headers=h, json={"type": "fake", "config": cfg, "credentials": {"password": "p"}})
    assert r.status_code == 200, r.text
    d = r.json()
    assert [n["name"] for n in d["networks"]] == ["dmz", "lan"] and [i["name"] for i in d["images"]] == ["ubuntu-24.04", "rocky-9"]
    # no password -> that list reports its error, the others still load
    d = c.post("/endpoints/discover", headers=h, json={"type": "fake", "config": cfg}).json()
    assert "not authenticated" in d["errors"]["images"] and d["networks"]
    # editing a saved endpoint: the stored password is used when the form leaves it empty
    ep = c.post("/endpoints", headers=h, json={"name": "t1", "type": "fake", "config": cfg,
                                               "credentials": {"password": "stored"}}).json()
    d = c.post("/endpoints/discover", headers=h, json={"type": "fake", "config": cfg, "endpoint_id": ep["id"]}).json()
    assert d["images"] and not d["errors"]
    assert c.post("/endpoints/discover", headers=plat.viewer, json={"type": "fake", "config": cfg}).status_code == 403
    assert c.post("/endpoints/discover", headers=h, json={"type": "nope"}).status_code in (400, 422)
    rows = c.get("/audit?q=discover", headers=h).json()
    assert rows and "stored" not in str(rows) and '"p"' not in str(rows)
    assert any((r["details"] or {}).get("request", {}).get("credentials") == "***" for r in rows)   # masked
