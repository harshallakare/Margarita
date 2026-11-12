"""Purpose: alert lifecycle + delivery. run() (called by the conductor every CHECK_SECONDS):
  1. rules.evaluate() -> the alerts that should be firing now
  2. reconcile with the alerts table: new -> firing row + notify; still firing -> refresh (and remind
     every repeat_hours while critical and not acknowledged); no longer true -> resolved + notify
  3. optional success notices for finished migrations / cutovers / deploys (channel opt-in)
  4. deliver through every enabled channel whose filters match (in a background thread, so a slow
     SMTP server never stalls the conductor); each channel records last_sent_at / last_error

Usage:
    m = AlertManager(repo, secrets, public_url="https://margarita.example.com")
    m.run()                     # conductor tick
    m.test_channel(row)         # "Send test email" button
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .providers import make_channel
from .rules import evaluate

log = logging.getLogger("vmigrate.alerts")

CHECK_SECONDS = 30
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
SUCCESS_KINDS = {"migration": "migration completed", "replica_cutover": "cutover completed",
                 "replica_deploy": "deploy completed"}


@dataclass
class Notice:
    event: str                     # firing | reminder | resolved | success
    severity: str
    subject: str
    body: str
    alert_id: str | None = None


def _utc(t: float | None) -> str:
    return datetime.fromtimestamp(t or 0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if t else "-"


def _dur(s: float) -> str:
    m = int(s // 60)
    return f"{m} min" if m < 120 else f"{m / 60:.1f} h"


class AlertManager:
    def __init__(self, repo: Any, secrets: Any, *, public_url: str | None = None, repeat_hours: float = 24,
                 background: bool = True, channel_factory: Callable[[dict[str, Any], str | None], Any] | None = None):
        self.repo, self.secrets = repo, secrets
        self.public_url = (public_url or "").rstrip("/") or None
        self.repeat_hours = repeat_hours
        self.background = background
        self.channel_factory = channel_factory or make_channel
        self._last_run = 0.0
        self._success_since = time.time()      # no flood of old successes after a restart
        self._send_lock = threading.Lock()

    # ------------------------------------------------------------------ evaluation
    def run(self, now: float | None = None, *, force: bool = False) -> dict[str, int]:
        now = now or time.time()
        if not force and now - self._last_run < CHECK_SECONDS:
            return {}
        self._last_run = now
        desired = {a.key: a for a in evaluate(self.repo, now)}
        firing = {r["key"]: r for r in self.repo.firing_alerts()}
        notices: list[Notice] = []
        stats = {"fired": 0, "resolved": 0, "reminded": 0}
        for key, a in desired.items():
            row = firing.get(key)
            if row is None:
                aid = self.repo.create_alert(key=key, kind=a.kind, severity=a.severity, project=a.project,
                                             migration_id=a.migration_id, execution_id=a.execution_id,
                                             object_name=a.object_name, title=a.title, detail=a.detail,
                                             first_at=min(a.since or now, now), notified_at=now)
                notices.append(self._notice("firing", a.severity, a.title, a.detail, aid, a.migration_id,
                                            a.execution_id, a.project, min(a.since or now, now), now))
                stats["fired"] += 1
                continue
            upd: dict[str, Any] = {"last_at": now}
            for f in ("title", "detail", "execution_id", "severity"):
                if getattr(a, f) != row.get(f):
                    upd[f] = getattr(a, f)
            due = (a.severity == "critical" and not row.get("ack_at") and self.repeat_hours > 0
                   and now - float(row.get("notified_at") or row["first_at"]) >= self.repeat_hours * 3600)
            if due:
                upd["notified_at"] = now
                notices.append(self._notice("reminder", a.severity, a.title, a.detail, row["id"], a.migration_id,
                                            a.execution_id, a.project, row["first_at"], now))
                stats["reminded"] += 1
            self.repo.update_alert(row["id"], **upd)
        for key, row in firing.items():
            if key in desired:
                continue
            self.repo.update_alert(row["id"], status="resolved", resolved_at=now, last_at=now)
            notices.append(self._notice("resolved", row["severity"], row["title"], row.get("detail") or "",
                                        row["id"], row.get("migration_id"), row.get("execution_id"),
                                        row.get("project"), row["first_at"], now))
            stats["resolved"] += 1
        notices += self._successes(now)
        if notices:
            self.deliver(notices)
        return stats

    def _successes(self, now: float) -> list[Notice]:
        since, self._success_since = self._success_since, now
        out: list[Notice] = []
        for x in self.repo.list_executions(status="completed"):
            fin = float(x.get("finished_at") or 0)
            if fin <= since or fin > now:
                continue
            if x.get("dry_run") or x["kind"] not in SUCCESS_KINDS:
                continue
            mig = self.repo.get_migration(x["migration_id"]) or {}
            title = f"{'replica' if mig.get('mode') == 'replica' else 'migration'} {mig.get('name', '?')}: " \
                    f"{SUCCESS_KINDS[x['kind']]}"
            took = _dur(fin - float(x.get("started_at") or x["created_at"]))
            out.append(self._notice("success", "info", title, f"finished in {took}", None, x["migration_id"],
                                    x["id"], mig.get("project"), fin, fin))
        return out

    # ------------------------------------------------------------------ messages
    def _link(self, migration_id: str | None, execution_id: str | None) -> list[str]:
        if not self.public_url:
            return ["(set VMIGRATE_PUBLIC_URL to get links to the UI in these emails)"]
        out = []
        mig = self.repo.get_migration(migration_id) if migration_id else None
        if mig:
            kind = "replicas" if mig["mode"] == "replica" else "migrations"
            out.append(f"Open:       {self.public_url}/ui/{kind}/{mig['id']}")
        if execution_id:
            out.append(f"Execution:  {self.public_url}/ui/executions/{execution_id}")
        out.append(f"All alerts: {self.public_url}/ui/alerts")
        return out

    def _notice(self, event: str, severity: str, title: str, detail: str, alert_id: str | None,
                migration_id: str | None, execution_id: str | None, project: str | None,
                since: float, now: float) -> Notice:
        label = {"firing": severity.upper(), "reminder": f"STILL {severity.upper()}", "resolved": "RESOLVED",
                 "success": "OK"}[event]
        lines = [f"{label}: {title}", ""]
        if event == "resolved":
            lines.append(f"Resolved:   {_utc(now)} (was firing for {_dur(now - since)})")
        elif event == "success":
            lines.append(f"Finished:   {_utc(now)}")
        else:
            lines.append(f"Since:      {_utc(since)}" + (f" ({_dur(now - since)})" if now - since >= 60 else ""))
        if project:
            lines.append(f"Project:    {project}")
        lines += ["", "Detail:", *("  " + ln for ln in (detail or "-").splitlines()[:40]), ""]
        lines += self._link(migration_id, execution_id)
        if event in ("firing", "reminder") and severity == "critical":
            lines += ["", f"Reminders repeat every {self.repeat_hours:g} h until it is resolved or acknowledged "
                          "on the Alerts page."]
        return Notice(event, severity, f"{label}: {title}", "\n".join(lines) + "\n", alert_id)

    # ------------------------------------------------------------------ delivery
    def channel(self, row: dict[str, Any]) -> Any:
        pw = None
        if row.get("secret_id"):
            pw = (self.secrets.get(row["secret_id"]) or {}).get("password")
        return self.channel_factory(dict(row["config"]), pw)

    @staticmethod
    def wants(row: dict[str, Any], n: Notice) -> bool:
        if not row.get("enabled", True):
            return False
        if n.event == "success":
            return bool(row.get("notify_success"))
        if n.event == "resolved" and not row.get("notify_resolved", True):
            return False
        return SEVERITY_RANK.get(n.severity, 2) >= SEVERITY_RANK.get(row.get("min_severity") or "warning", 1)

    def deliver(self, notices: list[Notice]) -> None:
        if self.background:
            threading.Thread(target=self._deliver, args=(notices,), name="alert-delivery", daemon=True).start()
        else:
            self._deliver(notices)

    def _deliver(self, notices: list[Notice]) -> None:
        with self._send_lock:
            for row in self.repo.list_channels():
                todo = [n for n in notices if self.wants(row, n)]
                if not todo:
                    continue
                err = None
                try:
                    ch = self.channel(row)
                    for n in todo:
                        ch.send(n.subject, n.body)
                except Exception as e:  # noqa: BLE001 - recorded on the channel, shown in Settings
                    err = f"{type(e).__name__}: {e}"[:1000]
                    log.warning("notification channel %s: %s", row["name"], err)
                self.repo.update_channel(row["id"], last_sent_at=time.time() if err is None else row.get("last_sent_at"),
                                         last_error=err)

    def test_channel(self, row: dict[str, Any]) -> None:
        """Send one test message now (raises on failure)."""
        self.send_test(self.channel(row), row["name"])
        self.repo.update_channel(row["id"], last_sent_at=time.time(), last_error=None)

    def send_test(self, ch: Any, name: str) -> None:
        body = ("This is a test message from Margarita alerting. If you can read it, alert emails will reach "
                "you.\n\n"
                f"Channel: {name}\nSent:    {_utc(time.time())}\n"
                + ("\n".join([""] + self._link(None, None)) if self.public_url else "") + "\n")
        ch.send("Test notification", body)
