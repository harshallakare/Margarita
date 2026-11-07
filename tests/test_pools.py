"""Purpose: worker pools — CRUD, membership via the pool:<name> tag, routing a migration to a pool,
single-use enrollment tokens, and auto-provisioned minions (cloud faked).

Usage: pytest tests/test_pools.py
"""

from __future__ import annotations

import time

from vmigrate.conductor.service import Conductor
from vmigrate.platform.pools import scale, user_data
from vmigrate.worker.service import Worker

from .conftest import fake_vm
from .test_platform import plat  # noqa: F401  (fixture)


class FakeCloud:
    def __init__(self, fail: bool = False):
        self.servers: dict[str, dict] = {}
        self.fail = fail

    def create(self, name, *, image, flavor, network, user_data, key_name=None, security_groups=None):
        if self.fail:
            raise RuntimeError("quota exceeded")
        sid = f"srv-{len(self.servers)}"
        self.servers[sid] = {"name": name, "image": image, "user_data": user_data}
        return sid

    def delete(self, sid):
        self.servers.pop(sid)


def test_pool_crud_membership_and_routing(plat):  # noqa: F811
    c, h = plat.c, plat.admin
    r = c.post("/pools", headers=h, json={"name": "vhi-nodes", "region": "pune", "min_size": 0, "max_size": 0})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert c.post("/pools", headers=h, json={"name": "vhi-nodes"}).status_code == 409
    assert c.post("/pools", headers=plat.viewer, json={"name": "x"}).status_code == 403
    assert c.patch(f"/pools/{pid}", headers=h, json={"min_size": 3, "max_size": 1}).status_code == 422

    src = c.post("/endpoints", headers=h, json={"name": "s", "type": "fake", "config": {"vms": [fake_vm(disks=2)]}}).json()
    tgt = c.post("/endpoints", headers=h, json={"name": "t", "type": "fake",
                                                "config": {"staging_dir": str(plat.tmp / "t"), "persist": True}}).json()
    mid = c.post("/migrations", headers=h, json={"name": "m", "source_endpoint_id": src["id"], "target_endpoint_id": tgt["id"],
                                                  "vm": "RIS-Sainath", "options": {"pool": "vhi-nodes"}}).json()["id"]
    xid = c.post(f"/migrations/{mid}/start", headers=h, json={"dry_run": False}).json()["id"]
    cond = Conductor(plat.svc)
    outsider = Worker(plat.svc, "w-other", region="pune", capabilities={"provider:fake"})
    member = Worker(plat.svc, "w-pool", region="pune", capabilities={"provider:fake", "pool:vhi-nodes"})
    outsider.heartbeat()
    cond.tick()
    x = c.get(f"/executions/{xid}", headers=h).json()
    assert x["region"] == "pune" and "pool:vhi-nodes" in x["waiting_reason"]      # pool region + tag applied
    assert outsider.tick() is None
    member.heartbeat()
    p = c.get(f"/pools/{pid}", headers=plat.viewer).json()
    assert [m_["id"] for m_ in p["members"]] == ["w-pool"] and p["tag"] == "pool:vhi-nodes"
    assert c.delete(f"/pools/{pid}", headers=h).status_code == 409             # live member
    cond.tick()
    assert member.tick() == xid
    assert c.get(f"/executions/{xid}", headers=h).json()["status"] == "completed"


def test_enrollment_tokens(plat):  # noqa: F811
    c, h = plat.c, plat.admin
    pid = c.post("/pools", headers=h, json={"name": "dc2", "region": "dc2"}).json()["id"]
    tok = c.post(f"/pools/{pid}/enroll-token", headers=h, json={"ttl_minutes": 10}).json()["token"]
    assert tok.startswith("mgj_")
    r = c.post("/enroll", json={"token": tok, "hostname": "minion-1"})
    assert r.status_code == 403 and "HTTPS" in r.text                         # TestClient speaks http
    plat.svc.settings.allow_http_enroll = True
    r = c.post("/enroll", json={"token": tok, "hostname": "minion-1"})
    assert r.status_code == 409 and "Postgres" in r.text                      # sqlite cannot be shared
    plat.svc.settings.database_url = "postgresql+psycopg://m:pw@db/margarita"
    r = c.post("/enroll", json={"token": tok, "hostname": "minion-1"}, headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["secret_key"] == plat.svc.settings.secret_key and body["region"] == "dc2" and body["tags"] == ["pool:dc2"]
    assert c.post("/enroll", json={"token": tok}).status_code == 401          # single use
    assert c.post("/enroll", json={"token": "mgj_nope"}).status_code == 401
    old, _ = plat.svc.repo.create_enroll_token(pid, -1, None)                 # already expired
    assert c.post("/enroll", json={"token": old}).status_code == 401


def test_minion_autoscaling(plat):  # noqa: F811
    c, h = plat.c, plat.admin
    cloud_ep = c.post("/endpoints", headers=h, json={"name": "vhi", "type": "fake", "config": {"staging_dir": str(plat.tmp / "x")}}).json()
    r = c.post("/pools", headers=h, json={"name": "mini", "min_size": 2, "max_size": 3, "provision": {
        "endpoint_id": cloud_ep["id"], "image": "rocky-9", "flavor": "m1.large", "network": "mgmt",
        "api_url": "https://margarita.example:8443"}})
    assert r.status_code == 201, r.text
    pl = plat.svc.repo.get_pool(r.json()["id"])
    cloud = FakeCloud()
    now = time.time()
    assert len(scale(plat.svc.repo, pl, cloud, alive_after=now - 90, now=now)) == 2
    ud = next(iter(cloud.servers.values()))["user_data"]
    assert "--enroll https://margarita.example:8443 --enroll-token mgj_" in ud and plat.svc.settings.secret_key not in ud
    pl = plat.svc.repo.get_pool(pl["id"])
    assert scale(plat.svc.repo, pl, cloud, alive_after=now - 90, now=now + 1) == []   # 2 still booting: enough
    # one minion joins as a worker; the other is stuck after the boot grace period -> a replacement
    name0 = pl["state"]["minions"][0]["name"]
    plat.svc.repo.worker_heartbeat("w-m0", name0, 1, None, capabilities=["provider:fake", "pool:mini"])
    later = now + 25 * 60
    pl = plat.svc.repo.get_pool(pl["id"])
    new = scale(plat.svc.repo, pl, cloud, alive_after=0, now=later)
    pl = plat.svc.repo.get_pool(pl["id"])
    assert len(new) == 1 and [m["status"] for m in pl["state"]["minions"]] == ["joined", "stuck", "booting"]
    # max_size reached: no more even if more were wanted
    plat.svc.repo.update_pool(pl["id"], min_size=10)
    assert scale(plat.svc.repo, plat.svc.repo.get_pool(pl["id"]), cloud, alive_after=0, now=later + 1) == []
    # failures are recorded, not raised
    pl = plat.svc.repo.get_pool(pl["id"])
    plat.svc.repo.update_pool(pl["id"], max_size=10)
    scale(plat.svc.repo, plat.svc.repo.get_pool(pl["id"]), FakeCloud(fail=True), alive_after=0, now=later + 2)
    assert "quota exceeded" in plat.svc.repo.get_pool(pl["id"])["state"]["last_error"]
    # the conductor refuses non-OpenStack endpoints for provisioning and records it
    Conductor(plat.svc).scale_pools(now=later + 120)
    assert "vhi/openstack" in plat.svc.repo.get_pool(pl["id"])["state"]["last_error"]


def test_user_data_points_at_raw_deploy_script():
    ud = user_data("https://api", "mgj_x", repo="https://github.com/acme/Margarita.git", branch="dev", full=True)
    assert "https://raw.githubusercontent.com/acme/Margarita/dev/scripts/deploy.sh" in ud and ud.rstrip().endswith(
        "rm -f /root/margarita-deploy.sh") and "--full" in ud
