"""Purpose: alerting — state-based rules (failed sync / migration, RPO missed, kept helper not removed,
no worker), alert lifecycle (fire, refresh, remind, acknowledge, resolve), channel filters, the email
channel's SMTP conversation, and the API (channels CRUD with save-time check, secrets never returned).

Usage: pytest tests/test_alerts.py
"""

from __future__ import annotations

import time

import pytest

from vmigrate.alerts.email import EmailChannel, validate
from vmigrate.core.errors import ConfigError

from .test_replica import rep  # noqa: F401  (fixture)
from .test_platform import plat  # noqa: F401  (fixture)

CFG = {"host": "smtp.example.test", "port": 587, "security": "starttls", "username": "alerts@example.test",
       "from_addr": "margarita@example.test", "to": ["ops@example.test"]}


class Outbox:
    """Channel factory for the manager: records what would have been mailed."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.checked: list[tuple[dict, str | None]] = []
        self.fail: str | None = None

    def __call__(self, cfg, password):
        box = self

        class Ch:
            def check(self):
                box.checked.append((cfg, password))
                if box.fail:
                    raise OSError(box.fail)

            def send(self, subject, body):
                if box.fail:
                    raise OSError(box.fail)
                box.sent.append((subject, body))
        return Ch()

    def subjects(self):
        return [s for s, _ in self.sent]


@pytest.fixture
def mgr(rep):  # noqa: F811
    m = rep.svc.alert_manager()
    m.background = False
    box = Outbox()
    m.channel_factory = box
    rep.repo_ = rep.svc.repo
    r = rep.c.post("/notification-channels", headers=rep.admin,
                   json={"name": "ops-mail", "config": CFG, "password": "smtp-test-pw"})
    assert r.status_code == 201, r.text
    rep.box, rep.mgr, rep.channel = box, m, r.json()
    return rep


def _opts(rep, **o):
    x = rep.c.patch(f"/replicas/{rep.rid}", headers=rep.admin, json={"options": {"retention": 2, **o}})
    assert x.status_code == 200, x.text


def test_rpo_alert_fires_reminds_acknowledges_and_resolves(mgr):
    rep, box, m = mgr, mgr.box, mgr.mgr
    _opts(rep, rpo_minutes=60)
    t0 = time.time()
    assert m.run(t0, force=True)["fired"] == 0                      # just created: within the objective
    assert m.run(t0 + 2 * 3600, force=True)["fired"] == 1
    subj, body = box.sent[-1]
    assert subj == "CRITICAL: replica ris-dr: recovery point objective missed"
    assert "no sync has completed" in body and "objective is 60 min" in body and "Reminders repeat" in body
    s = rep.c.get("/alerts/summary", headers=rep.viewer).json()
    assert s == {"firing": 1, "critical": 1, "unacknowledged": 1}
    assert m.run(t0 + 3 * 3600, force=True) == {"fired": 0, "resolved": 0, "reminded": 0}     # no repeat yet
    assert m.run(t0 + 27 * 3600, force=True)["reminded"] == 1                                 # daily reminder
    assert box.subjects()[-1].startswith("STILL CRITICAL: replica ris-dr")
    a = rep.c.get("/alerts?status=firing", headers=rep.viewer).json()[0]
    assert rep.c.post(f"/alerts/{a['id']}/ack", headers=rep.viewer).status_code == 403
    acked = rep.c.post(f"/alerts/{a['id']}/ack", headers=rep.admin).json()
    assert acked["ack_by"] == "admin"
    assert m.run(t0 + 60 * 3600, force=True)["reminded"] == 0                                 # acknowledged: quiet
    assert rep.run("execute")["status"] == "completed"                                       # a sync completes
    assert m.run(time.time() + 60, force=True)["resolved"] == 1
    assert box.subjects()[-1] == "RESOLVED: replica ris-dr: recovery point objective missed"
    assert rep.c.get("/alerts?status=firing", headers=rep.viewer).json() == []
    assert rep.c.get("/alerts?status=resolved", headers=rep.viewer).json()[0]["resolved_at"]
    # newest sync older than the objective again -> a NEW alert episode with the age in the detail
    assert m.run(time.time() + 3 * 3600, force=True)["fired"] == 1
    assert "newest recovery point is" in box.sent[-1][1]


def test_failed_sync_alerts_once_and_resolves_on_success_dry_runs_ignored(mgr):
    rep, box, m = mgr, mgr.box, mgr.mgr
    assert rep.run("execute")["status"] == "completed"
    rep.set_source(generation=1, fail_read_at=1 << 20)
    assert rep.run("execute", {"dry_run": True})["status"] == "completed"        # dry runs read no disk data
    assert rep.run("execute")["status"] == "failed"
    assert m.run(force=True)["fired"] == 1
    subj, body = box.sent[-1]
    assert subj == "CRITICAL: replica ris-dr: sync failed"
    assert "injected read failure" in body and "Detail:" in body                 # the execution's error
    assert rep.run("execute")["status"] == "failed"                              # fails again: same alert, no mail
    n = len(box.sent)
    assert m.run(force=True) == {"fired": 0, "resolved": 0, "reminded": 0} and len(box.sent) == n
    rep.set_source(generation=1)
    assert rep.run("execute")["status"] == "completed"
    assert m.run(force=True)["resolved"] == 1 and box.subjects()[-1] == "RESOLVED: replica ris-dr: sync failed"


def test_channel_filters_success_notices_and_delivery_errors(mgr):
    rep, box, m = mgr, mgr.box, mgr.mgr
    cid = rep.channel["id"]
    assert rep.c.patch(f"/notification-channels/{cid}", headers=rep.admin,
                       json={"min_severity": "critical", "notify_resolved": False, "notify_success": True}).status_code == 200
    repo = rep.svc.repo
    xid = rep.run("execute")["id"]                                              # syncs never send success notices
    mid = repo.get_execution(xid)["migration_id"]
    rep.svc.repo.create_alert(key="manual:1", kind="test", severity="warning", project="default", migration_id=mid,
                              title="a warning", detail="d", notified_at=time.time())
    m._success_since = 0
    m.run(force=True)                                                            # resolves the stray warning
    assert box.sent == []                                                        # warning + resolved filtered out
    ok = rep.c.post(f"/replicas/{rep.rid}/deploy", headers=rep.admin, json={"dry_run": False})
    rep.drive()
    m.run(time.time() + 1, force=True)
    assert box.subjects() == ["OK: replica ris-dr: deploy completed"], (ok.json(), box.subjects())
    box.fail = "connection refused"
    rep.set_source(generation=1, fail_read_at=1 << 20)
    assert rep.run("execute")["status"] == "failed"
    m.run(force=True)
    ch = rep.c.get("/notification-channels", headers=rep.admin).json()[0]
    assert "connection refused" in ch["last_error"]                              # shown in Settings


def test_channel_api_checks_before_saving_and_never_returns_the_password(mgr):
    rep, box = mgr, mgr.box
    ch = rep.channel
    assert ch["has_password"] and "password" not in ch and "smtp-test-pw" not in str(ch)
    assert box.checked[-1] == (CFG | {"verify_tls": True, "subject_prefix": "[Margarita]"}, "smtp-test-pw")
    assert rep.c.get("/notification-channels", headers=rep.viewer).status_code == 403
    box.fail = "535 authentication failed"
    bad = rep.c.post("/notification-channels", headers=rep.admin, json={"name": "b", "config": CFG, "password": "x"})
    assert bad.status_code == 422 and "535 authentication failed" in bad.text
    assert rep.c.post("/notification-channels", headers=rep.admin,
                      json={"name": "b", "config": CFG, "skip_check": True}).status_code == 201
    t = rep.c.post(f"/notification-channels/{ch['id']}/test", headers=rep.admin)
    assert t.status_code == 422 and "535" in t.text
    box.fail = None
    assert rep.c.post(f"/notification-channels/{ch['id']}/test", headers=rep.admin).json()["ok"]
    assert box.sent[-1][0] == "Test notification"
    # keeping the stored password on edits; audit trail masks it
    assert rep.c.patch(f"/notification-channels/{ch['id']}", headers=rep.admin,
                       json={"config": {**CFG, "port": 2525}}).status_code == 200
    assert box.checked[-1][1] == "smtp-test-pw" and box.checked[-1][0]["port"] == 2525
    audit = rep.c.get("/audit?object_type=notification_channel", headers=rep.admin).json()
    assert audit and "smtp-test-pw" not in str(audit)
    bad_cfg = rep.c.post("/notification-channels", headers=rep.admin,
                         json={"name": "c", "config": {**CFG, "security": "tls13"}})
    assert bad_cfg.status_code == 422
    assert rep.c.delete(f"/notification-channels/{ch['id']}", headers=rep.admin).status_code == 204


def test_waiting_and_helper_rules(mgr):
    rep, m = mgr, mgr.mgr
    repo = rep.svc.repo
    st = dict(repo.get_migration(rep.rid)["state"] or {"kind": "replica", "name": "ris-dr", "source": "x", "target": "y"})
    st["helper"] = {"name": "vmigrate-helper-x", "last_reap_error": "ProviderError: 409 conflict"}
    repo.save_state(rep.rid, st)
    xid = repo.create_execution(rep.rid, kind="replica_sync", dry_run=False, user_id=None)
    repo.set_execution(xid, status="queued", waiting_reason="no live worker in region 'dc2'",
                       created_at=time.time() - 3600)
    m.run(force=True)
    kinds = {a["kind"]: a for a in rep.c.get("/alerts?status=firing", headers=rep.admin).json()}
    assert set(kinds) == {"helper_cleanup_failed", "no_worker"}
    assert "409 conflict" in kinds["helper_cleanup_failed"]["detail"] and "dc2" in kinds["no_worker"]["detail"]
    assert all(s.startswith("WARNING: replica ris-dr") for s in mgr.box.subjects())


class FakeSmtp:
    log: list = []

    def __init__(self, host, port, timeout=None, context=None):
        FakeSmtp.log = [("connect", host, port, context is not None)]

    def ehlo(self): FakeSmtp.log.append("ehlo")
    def starttls(self, context=None): FakeSmtp.log.append("starttls")
    def login(self, u, p): FakeSmtp.log.append(("login", u, p))
    def send_message(self, msg): FakeSmtp.log.append(("send", msg["Subject"], msg["To"], msg.get_content()))
    def quit(self): FakeSmtp.log.append("quit")


def test_email_channel_smtp_conversation():
    ch = EmailChannel(CFG, "pw", smtp_factory=FakeSmtp)
    ch.send("CRITICAL: x", "body text\n")
    assert FakeSmtp.log[:4] == [("connect", "smtp.example.test", 587, False), "ehlo", "starttls", "ehlo"]
    assert FakeSmtp.log[4] == ("login", "alerts@example.test", "pw")
    assert FakeSmtp.log[5][:3] == ("send", "[Margarita] CRITICAL: x", "ops@example.test") and FakeSmtp.log[-1] == "quit"
    EmailChannel({**CFG, "security": "ssl", "port": 465, "username": None}, None, smtp_factory=FakeSmtp).check()
    assert FakeSmtp.log == [("connect", "smtp.example.test", 465, True), "ehlo", "quit"]   # TLS from the start, no login
    assert validate({**CFG, "to": ["not-an-address"], "port": 0})
    with pytest.raises(ConfigError, match="to must list"):
        EmailChannel({**CFG, "to": []})
