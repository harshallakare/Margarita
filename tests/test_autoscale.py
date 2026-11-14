"""Purpose: the autoscaling worker supervisor (one process per job between min and max, gated by host
load), its Settings API, worker removal and enrollment tokens, and the password APIs + reset CLI.

Usage: pytest tests/test_autoscale.py
"""

from __future__ import annotations

import threading

import pytest

from vmigrate.cli import platform_cli
from vmigrate.core.routing import waiting_reason
from vmigrate.platform.pools import members
from vmigrate.security.auth import verify_password
from vmigrate.worker.autoscale import (DEFAULTS, KEY_DEFAULTS, KEY_HOST, Load, Sampler, Supervisor, decide,
                                       effective, validate)
from vmigrate.worker.service import Worker

from .test_platform import PW, plat  # noqa: F401  (fixture)


# ------------------------------------------------------------------ pure decisions
def cfg(**kw):
    return effective({**DEFAULTS, "max_slots": 4, "spawn_interval_s": 30, **kw}, None, cpu_count=8)


def test_decide_rules():
    idle = Load(cpu=10, mem_free=70, disk_free_gib=500)
    assert decide(cfg(), running=0, starting=0, claimable=0, load=idle, since_spawn=999)[0] is False
    assert decide(cfg(), running=0, starting=0, claimable=1, load=idle, since_spawn=0)[0] is True      # below min: now
    ok, why = decide(cfg(), running=1, starting=0, claimable=1, load=idle, since_spawn=5)
    assert not ok and "waiting" in why                                                                # spacing
    assert decide(cfg(), running=1, starting=0, claimable=1, load=idle, since_spawn=60)[0] is True
    ok, why = decide(cfg(), running=1, starting=0, claimable=1, load=Load(cpu=95), since_spawn=60)
    assert not ok and "CPU 95%" in why
    ok, why = decide(cfg(), running=1, starting=0, claimable=1, load=Load(mem_free=5), since_spawn=60)
    assert not ok and "memory" in why
    ok, why = decide(cfg(), running=1, starting=0, claimable=1, load=Load(disk_free_gib=3), since_spawn=60)
    assert not ok and "staging" in why
    ok, why = decide(cfg(), running=4, starting=0, claimable=5, load=idle, since_spawn=999)
    assert not ok and "maximum of 4" in why
    # a busy host still gets its minimum
    assert decide(cfg(min_slots=2), running=1, starting=0, claimable=1, load=Load(cpu=99), since_spawn=0)[0] is True
    # children still starting may be about to claim the queued jobs
    assert decide(cfg(), running=1, starting=1, claimable=1, load=idle, since_spawn=60)[0] is False
    ok, why = decide(cfg(paused=True), running=0, starting=0, claimable=3, load=idle, since_spawn=999)
    assert not ok and "paused" in why


def test_effective_layers_and_validation():
    e = effective({"max_slots": 6, "cpu_max_pct": 70}, {"max_slots": 2}, cpu_count=16)
    assert e["max_slots"] == 2 and e["cpu_max_pct"] == 70 and not e["max_slots_auto"]
    auto = effective(None, None, cpu_count=8)
    assert auto["max_slots"] == 4 and auto["max_slots_auto"] and auto["min_slots"] == 1
    assert effective(None, None, cpu_count=2)["max_slots"] == 1
    assert effective({"min_slots": 5}, None, cpu_count=4)["min_slots"] == 2          # clamped to the max
    with pytest.raises(ValueError):
        validate({"min_slots": 3, "max_slots": 1})
    with pytest.raises(ValueError):
        validate({"cpu_max_pct": 400})
    with pytest.raises(ValueError):
        validate({"bogus": 1})
    assert validate({"max_slots": "3", "paused": 1}) == {"max_slots": 3, "paused": True}


def test_sampler_reads_proc(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "meminfo").write_text("MemTotal:       16000000 kB\nMemAvailable:    4000000 kB\n")
    t = [0.0]
    s = Sampler(tmp_path, proc=proc, clock=lambda: t[0])
    (proc / "stat").write_text("cpu  100 0 100 800 0 0 0 0 0 0\n")
    s.sample()
    t[0] = 5
    (proc / "stat").write_text("cpu  400 0 200 900 0 0 0 0 0 0\n")       # +400 busy, +100 idle
    load = s.sample()
    assert round(load.cpu) == 80 and round(load.mem_free) == 25 and load.disk_free_gib > 0


# ------------------------------------------------------------------ supervisor
class Proc:
    def __init__(self):
        self.rc = None
        self.pid = 4242
        self.signals = []

    def poll(self):
        return self.rc

    def send_signal(self, s):
        self.signals.append(s)
        self.rc = -s

    def wait(self, timeout=None):
        return self.rc


class Quiet:
    def sample(self):
        return Load(cpu=5, mem_free=80, disk_free_gib=900)


def _queue(plat, n=1):  # noqa: F811
    src, tgt = plat.endpoints()
    out = []
    for i in range(n):
        mid = plat.c.post("/migrations", headers=plat.admin, json={
            "name": f"m{i}", "source_endpoint_id": src, "target_endpoint_id": tgt, "vm": "RIS-Sainath"}).json()["id"]
        out.append(plat.c.post(f"/migrations/{mid}/start", headers=plat.admin, json={"dry_run": True}).json()["id"])
    plat.conductor.tick()
    return out


def test_supervisor_scales_between_min_and_max(plat):  # noqa: F811
    _queue(plat, 3)
    procs = []
    t = [100.0]

    def spawn(parent):
        procs.append(Proc())
        return procs[-1]
    sup = Supervisor(plat.svc, spawn=spawn, sampler=Quiet(), clock=lambda: t[0], hostname="template",
                     capabilities={"provider:fake"}, cpu_count=8)
    plat.svc.repo.set_setting(KEY_DEFAULTS, {"max_slots": 2, "spawn_interval_s": 30})
    info = sup.tick()
    assert len(procs) == 1 and info["running"] == 1 and info["queued"] == 3
    t[0] += 16                                       # past STARTING_S, before the spawn interval
    assert sup.tick()["decision"].startswith("waiting")
    t[0] += 20
    sup.tick()
    assert len(procs) == 2
    t[0] += 60
    assert "maximum of 2" in sup.tick()["decision"]          # max reached
    procs[0].rc = 0                                          # a job finished
    t[0] += 60
    sup.tick()
    assert len(procs) == 3
    # host override: pause drains (no new jobs), running ones are left alone
    plat.svc.repo.set_setting(KEY_HOST + "template", {"paused": True})
    procs[1].rc = 0
    t[0] += 60
    info = sup.tick()
    assert len(procs) == 3 and info["paused"] and "paused" in info["decision"]
    w = plat.svc.repo.get_worker(sup.id)
    assert w["info"]["role"] == "supervisor" and w["info"]["running"] == 1
    sup.shutdown(timeout=1)
    assert procs[2].signals and plat.svc.repo.get_worker(sup.id) is None


def test_supervisor_child_runs_the_job_end_to_end(plat, monkeypatch):  # noqa: F811
    (xid,) = _queue(plat)
    threads = []

    def spawn(parent):
        monkeypatch.setenv("VMIGRATE_WORKER_PARENT", parent)
        w = Worker(plat.svc, capabilities={"provider:fake"})
        assert w.info == {"role": "job", "parent": parent}
        th = threading.Thread(target=w.run_once)
        th.start()
        threads.append(th)

        class P:
            def poll(self):
                return None if th.is_alive() else 0
        return P()
    sup = Supervisor(plat.svc, spawn=spawn, sampler=Quiet(), hostname="template", capabilities={"provider:fake"},
                     cpu_count=4)
    sup.tick()
    for th in threads:
        th.join(30)
    assert plat.c.get(f"/executions/{xid}", headers=plat.admin).json()["status"] == "completed"
    sup.tick()
    assert sup.last["running"] == 0 and sup.last["queued"] == 0
    # the job process de-registered itself; only the supervisor row is left
    assert [w["id"] for w in plat.svc.repo.list_workers()] == [sup.id]


def test_supervisor_rows_in_pools_and_waiting_reasons():
    now = 1000.0
    sup = {"id": "s1", "hostname": "h", "heartbeat_at": now, "capabilities": ["pool:a", "provider:fake"],
           "info": {"role": "supervisor", "running": 2}}
    job = {"id": "j1", "hostname": "h", "heartbeat_at": now, "capabilities": ["pool:a"], "current_execution": "x",
           "info": {"role": "job", "parent": "s1"}}
    m = members({"name": "a"}, [sup, job], alive_after=0)
    assert [x["id"] for x in m] == ["s1"] and m[0]["busy"]
    paused = {**sup, "info": {"role": "supervisor", "paused": True}}
    assert "paused" in waiting_reason([paused], {"caps": ["provider:fake"]}, 0)
    assert waiting_reason([sup], {"caps": ["provider:fake"]}, 0) is None


# ------------------------------------------------------------------ worker settings API
def test_worker_settings_api(plat):  # noqa: F811
    c, h, v = plat.c, plat.admin, plat.viewer
    assert c.put("/workers/settings/defaults", headers=v, json={"max_slots": 3}).status_code == 403
    assert c.put("/workers/settings/defaults", headers=h, json={"max_slots": 3, "cpu_max_pct": 75}).status_code == 200
    assert c.put("/workers/settings/defaults", headers=h, json={"min_slots": 4, "max_slots": 2}).status_code == 422
    assert c.put("/workers/settings/hosts/template", headers=h, json={"max_slots": 6, "paused": True}).status_code == 200
    sup = Supervisor(plat.svc, spawn=lambda p: Proc(), sampler=Quiet(), hostname="template", capabilities=set(), cpu_count=8)
    sup.tick()
    st = c.get("/workers/settings", headers=v).json()
    assert st["defaults"] == {"max_slots": 3, "cpu_max_pct": 75}
    assert st["hosts"]["template"] == {"max_slots": 6, "paused": True}
    assert st["effective"]["template"]["max_slots"] == 6 and st["effective"]["template"]["cpu_max_pct"] == 75
    assert c.delete("/workers/settings/hosts/template", headers=h).status_code == 204
    assert c.get("/workers/settings", headers=v).json()["hosts"] == {}
    rows = c.get("/workers", headers=v).json()
    assert rows[0]["info"]["role"] == "supervisor"
    acts = [a["action"] for a in c.get("/audit", headers=h).json()]
    assert "worker.settings_update" in acts and "worker.settings_reset" in acts


def test_remove_worker_and_enroll_token(plat):  # noqa: F811
    c, h = plat.c, plat.admin
    plat.worker.heartbeat()
    assert c.delete("/workers/w1", headers=h).status_code == 409                    # alive
    plat.svc.repo.worker_heartbeat("old", "gone", 1, None)
    with plat.svc.repo.engine.begin() as conn:
        from vmigrate.db import schema as s
        conn.execute(s.workers.update().where(s.workers.c.id == "old").values(heartbeat_at=1.0))
    assert c.delete("/workers/old", headers=plat.viewer).status_code == 403
    assert c.delete("/workers/old", headers=h).status_code == 204
    assert c.delete("/workers/old", headers=h).status_code == 404
    tok = c.post("/workers/enroll-token", headers=h, json={"ttl_minutes": 10}).json()
    assert tok["token"].startswith("mgj_")


# ------------------------------------------------------------------ passwords
def test_change_own_password(plat):  # noqa: F811
    c = plat.c
    other = {"Authorization": "Bearer " + c.post("/auth/token", json={"username": "viewer", "password": PW}).json()["token"]}
    r = c.post("/auth/password", headers=plat.viewer, json={"current_password": "wrong-password!!", "new_password": "x" * 12})
    assert r.status_code == 403
    assert c.post("/auth/password", headers=plat.viewer, json={"current_password": PW, "new_password": "short"}).status_code == 422
    assert c.post("/auth/password", headers=plat.viewer, json={"current_password": PW, "new_password": PW}).status_code == 422
    r = c.post("/auth/password", headers=plat.viewer, json={"current_password": PW, "new_password": "brand-new-pass-123"})
    assert r.status_code == 200 and r.json()["other_sessions_signed_out"] == 1
    assert c.get("/auth/me", headers=plat.viewer).status_code == 200                # this session stays
    assert c.get("/auth/me", headers=other).status_code == 401                      # the other one ended
    assert c.post("/auth/token", json={"username": "viewer", "password": PW}).status_code == 401
    assert c.post("/auth/token", json={"username": "viewer", "password": "brand-new-pass-123"}).status_code == 200
    audit = c.get("/audit", headers=plat.admin).json()
    row = next(a for a in audit if a["action"] == "auth.password_change")
    assert "brand-new" not in str(row) and PW not in str(row)                       # never logged


def test_admin_user_management(plat):  # noqa: F811
    c, h = plat.c, plat.admin
    users = {u["username"]: u for u in c.get("/users", headers=h).json()}
    vid, aid = users["viewer"]["id"], users["admin"]["id"]
    assert c.put(f"/users/{vid}/password", headers=plat.viewer, json={"new_password": "x" * 12}).status_code == 403
    assert c.put(f"/users/{vid}/password", headers=h, json={"new_password": "reset-by-admin-1"}).status_code == 200
    assert c.get("/auth/me", headers=plat.viewer).status_code == 401                # signed out
    assert c.post("/auth/token", json={"username": "viewer", "password": "reset-by-admin-1"}).status_code == 200
    assert c.patch(f"/users/{aid}", headers=h, json={"role": "operator"}).status_code == 409   # last admin
    assert c.patch(f"/users/{aid}", headers=h, json={"disabled": True}).status_code == 409
    r = c.patch(f"/users/{vid}", headers=h, json={"role": "operator", "disabled": True})
    assert r.status_code == 200 and r.json()["role"] == "operator" and r.json()["disabled"] and "password_hash" not in r.json()
    assert c.post("/auth/token", json={"username": "viewer", "password": "reset-by-admin-1"}).status_code == 401


def test_reset_password_cli(plat, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(platform_cli, "_services", lambda: plat.svc)
    repo = plat.svc.repo
    admin = repo.get_user_by_name("admin")
    repo.update_user(admin["id"], disabled=True)
    assert platform_cli.main(["reset-password", "nobody", "--generate"]) == 2
    assert "users: admin, viewer" in capsys.readouterr().err
    monkeypatch.setenv("NEWPW", "short")
    assert platform_cli.main(["reset-password", "admin", "--password-env", "NEWPW"]) == 2
    monkeypatch.setenv("NEWPW", "recovered-admin-pass")
    assert platform_cli.main(["reset-password", "admin", "--password-env", "NEWPW"]) == 0
    out = capsys.readouterr().out
    assert "account enabled" in out and "1 session(s) signed out" in out
    u = repo.get_user_by_name("admin")
    assert not u["disabled"] and verify_password("recovered-admin-pass", u["password_hash"])
    assert plat.c.get("/auth/me", headers=plat.admin).status_code == 401
    assert platform_cli.main(["reset-password", "admin", "--generate"]) == 0
    pw = capsys.readouterr().out.split("new password: ")[1].split()[0]
    assert len(pw) >= 12 and verify_password(pw, repo.get_user_by_name("admin")["password_hash"])
    assert any(a["action"] == "user.password_reset" and a["actor"] == "system:cli" for a in repo.list_audit(limit=50))


# ------------------------------------------------------------------ manual mode
def test_manual_mode_ignores_load_and_respects_threshold():
    busy = Load(cpu=99, mem_free=1, disk_free_gib=0)
    man = cfg(mode="manual", manual_slots=3)
    assert decide(man, running=2, starting=0, claimable=1, load=busy, since_spawn=0) == (True, "manual: 1 of 3 worker(s) free")
    ok, why = decide(man, running=3, starting=0, claimable=4, load=Load(), since_spawn=999)
    assert not ok and "all busy" in why
    ok, why = decide(cfg(mode="manual", manual_slots=0), running=0, starting=0, claimable=1, load=Load(), since_spawn=999)
    assert not ok and "0 workers" in why
    assert decide(cfg(mode="manual", manual_slots=3, paused=True), running=0, starting=0, claimable=1, load=Load(),
                  since_spawn=999)[0] is False
    # a stored value above the threshold (e.g. threshold lowered later) is clamped to it
    assert effective({"mode": "manual", "manual_slots": 9, "max_slots": 4}, None, cpu_count=8)["manual_slots"] == 4
    with pytest.raises(ValueError):
        validate({"manual_slots": 5, "max_slots": 4})
    with pytest.raises(ValueError):
        validate({"mode": "turbo"})


def test_manual_mode_api(plat):  # noqa: F811
    c, h = plat.c, plat.admin
    sup = Supervisor(plat.svc, spawn=lambda p: Proc(), sampler=Quiet(), hostname="template", capabilities=set(), cpu_count=8)
    sup.tick()                                                   # registers 8 CPUs -> automatic threshold 4
    r = c.put("/workers/settings/hosts/template", headers=h, json={"mode": "manual", "manual_slots": 5})
    assert r.status_code == 422 and "threshold (4)" in r.text
    assert c.put("/workers/settings/hosts/template", headers=h,
                 json={"mode": "manual", "manual_slots": 6, "max_slots": 8}).status_code == 200
    info = sup.tick()
    assert info["mode"] == "manual" and info["manual_slots"] == 6 and info["max_slots"] == 8
    assert c.put("/workers/settings/hosts/template", headers=h, json={"mode": "bogus"}).status_code == 422
