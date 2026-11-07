"""Purpose: the audit trail (every change recorded, secrets masked, tamper-evident, admin only) and the
investigation tools (log search across runs, tracebacks, event history, diagnostics bundle).

Usage: pytest tests/test_audit_logs.py
"""

from __future__ import annotations

import io
import json
import zipfile

from sqlalchemy import text

from vmigrate.conductor.service import Conductor
from vmigrate.security.audit import describe, redact

from .test_platform import PW, plat  # noqa: F401  (fixture)


def test_describe_and_redact():
    assert describe("POST", "/endpoints") == ("endpoint.create", "endpoint")
    assert describe("PATCH", "/endpoints/{eid}") == ("endpoint.update", "endpoint")
    assert describe("POST", "/replicas/{rid}/cutover") == ("replica.cutover", "replica")
    assert describe("POST", "/replicas/{rid}/schedules") == ("schedule.create", "schedule")
    assert describe("DELETE", "/pools/{pid}/minions/{server_id}") == ("minion.delete", "minion")
    assert describe("POST", "/auth/token") == ("auth.login", "user")
    r = redact({"username": "u", "password": "p", "config": {"host": "h", "auth_url": "https://k",
                                                            "ssh_private_key": "-----BEGIN", "api_key": "x"},
                "credentials": {"password": "s"}, "empty_token": ""})
    assert r["password"] == "***" and r["credentials"] == "***" and r["config"]["ssh_private_key"] == "***"
    assert r["config"]["api_key"] == "***" and r["config"]["host"] == "h" and r["config"]["auth_url"] == "https://k"
    assert r["empty_token"] == ""                                            # nothing to hide


def test_every_change_is_audited_with_secrets_masked(plat):  # noqa: F811
    c, h = plat.c, plat.admin
    assert c.post("/auth/token", json={"username": "admin", "password": "wrong-password"}).status_code == 401
    src, tgt = plat.endpoints()
    mid = plat.migration(src, tgt)
    c.patch(f"/endpoints/{src}", headers=h, json={"description": "prod nutanix"})
    assert c.post(f"/migrations/{mid}/start", headers=plat.viewer, json={"dry_run": True}).status_code == 403
    xid = c.post(f"/migrations/{mid}/start", headers=h, json={"dry_run": True}).json()["id"]
    rows = c.get("/audit", headers=h).json()
    acts = [(r["action"], r["outcome"], r["actor"]) for r in rows]
    assert ("auth.login", "denied", "admin") in acts                         # failed login recorded
    assert ("endpoint.create", "success", "admin") in acts and ("endpoint.update", "success", "admin") in acts
    assert ("migration.create", "success", "admin") in acts
    assert ("migration.start", "denied", "viewer") in acts                   # refused attempts too
    start = next(r for r in rows if r["action"] == "migration.start" and r["outcome"] == "success")
    assert start["object_id"] == mid and start["object_type"] == "migration" and start["ip"]
    created = next(r for r in rows if r["action"] == "endpoint.create" and r["object_name"] == "ahv")
    assert created["object_id"] == src                                       # id taken from the 201 response
    blob = json.dumps(rows)
    assert "s3cret-src" not in blob and "wrong-password" not in blob and PW not in blob   # never stored
    assert created["details"]["request"]["credentials"] == "***"
    sid = c.post("/endpoints", headers=h, json={"name": "temp-ep", "type": "fake", "config": {"vms": []}}).json()["id"]
    assert c.delete(f"/endpoints/{sid}", headers=h).status_code == 204
    gone = c.get("/audit?action=endpoint.delete", headers=h).json()[0]
    assert gone["object_id"] == sid and gone["object_name"] == "temp-ep"
    upd = c.get("/audit?action=endpoint.update", headers=h).json()[0]
    assert upd["object_name"] == "ahv"                                       # name looked up for updates
    r = c.get(f"/executions/{xid}", headers=h)
    assert r.headers["X-Request-Id"]
    # filters, admin only, CSV export
    assert all(x["object_id"] == mid for x in c.get(f"/audit?object_id={mid}", headers=h).json())
    assert c.get("/audit", headers=plat.viewer).status_code == 403
    csv = c.get("/audit/export", headers=h)
    assert csv.status_code == 200 and csv.text.startswith("id,ts,time_utc,actor")
    assert c.get("/audit?action=audit.export", headers=h).json()[0]["actor"] == "admin"   # exporting is audited


def test_audit_chain_detects_tampering_and_system_actions(plat):  # noqa: F811
    c, h = plat.c, plat.admin
    plat.endpoints()
    assert c.get("/audit/verify", headers=h).json()["ok"]
    cond = Conductor(plat.svc)
    cond.audit("replica.execute", "replica", "r1", "db-dr", {"cron": "0 2 * * *"})
    sys_row = c.get("/audit?actor=system:conductor", headers=h).json()[0]
    assert sys_row["action"] == "replica.execute" and sys_row["actor_role"] == "system"
    with plat.svc.repo.engine.begin() as conn:
        conn.execute(text("UPDATE audit_log SET actor='someone-else' WHERE id=1"))
    v = c.get("/audit/verify", headers=h).json()
    assert not v["ok"] and v["broken_at"] == 1 and "changed" in v["reason"]
    assert all(m not in [r.path for r in plat.c.app.routes] for m in ("/audit/{aid}",))  # no edit/delete API


def test_log_search_tracebacks_events_and_diagnostics(plat):  # noqa: F811
    c, h = plat.c, plat.admin
    src, tgt = plat.endpoints(src_extra={"fail_read_at": 1 << 20})
    mid = plat.migration(src, tgt)
    xid = c.post(f"/migrations/{mid}/start", headers=h, json={"dry_run": False}).json()["id"]
    plat.drive()
    x = c.get(f"/executions/{xid}", headers=h).json()
    assert x["status"] == "failed"
    errs = c.get("/logs?level=ERROR", headers=h).json()
    assert errs and all(r["level"] == "ERROR" for r in errs)
    tb = next(r for r in errs if "Traceback" in r["message"])               # full traceback kept
    assert tb["migration_name"] == "ris" and tb["kind"] == "migration" and tb["logger"]
    assert c.get(f"/logs?q=injected&migration_id={mid}", headers=h).json()
    assert c.get("/logs?q=zzz-nothing-like-this", headers=h).json() == []
    ev = c.get(f"/events/history?migration_id={mid}&type=execution.", headers=h).json()
    assert ev and all(e["type"].startswith("execution.") for e in ev)
    z = c.get(f"/executions/{xid}/diagnostics", headers=plat.viewer)
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(z.content))
    names = set(zf.namelist())
    assert {"execution.json", "tasks.json", "logs.txt", "events.json", "endpoints.json", "versions.json",
            "audit.json", "state.json"} <= names
    allt = "".join(zf.read(n).decode() for n in names)
    assert "s3cret-src" not in allt and "s3cret-tgt" not in allt             # credentials never in the bundle
    assert "Traceback" in zf.read("logs.txt").decode()
    assert json.loads(zf.read("endpoints.json"))["source"]["has_credentials"] is True
    assert c.get(f"/audit?action=execution.diagnostics", headers=h).json()[0]["actor"] == "viewer"
