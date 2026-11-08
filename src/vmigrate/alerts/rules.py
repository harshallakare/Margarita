"""Purpose: alert rules. evaluate() looks at the platform's current state and returns every alert that
SHOULD be firing right now; the manager reconciles that set with the alerts table (new -> fire +
notify, gone -> resolve + notify). Rules are state-based, so a restart of the conductor neither loses
nor duplicates alerts.

Rules (key -> severity):
  failed:<kind>:<migration>  critical  the latest real (non dry-run) execution of that kind failed;
                                       resolves when a later one completes (or the object is deleted)
  rpo:<replica>              critical  newest completed sync older than options.rpo_minutes
  helper:<replica>           warning   a kept helper VM could not be removed (conductor reaper)
  waiting:<execution>        warning   queued > WAIT_MINUTES with no live worker able to run it

Usage:
    for a in evaluate(repo, time.time()): print(a.key, a.title)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

WAIT_MINUTES = 10
FINISHED = ("completed", "failed", "canceled")

# execution kinds whose failures alert, and how they are named; syncs and cutovers share one alert
# (both bring the replica up to date: a completed cutover resolves a failed sync)
KIND_GROUP = {"migration": "migration", "replica_sync": "sync", "replica_cutover": "sync",
              "replica_deploy": "deploy", "replica_delete": "delete"}
GROUP_TITLE = {"migration": "migration failed", "sync": "sync failed", "deploy": "deploy failed",
               "delete": "delete replica disks failed"}


@dataclass
class Alert:
    key: str
    kind: str
    severity: str
    title: str
    detail: str
    project: str | None = None
    migration_id: str | None = None
    execution_id: str | None = None
    object_name: str | None = None
    since: float | None = None          # when the condition started (first_at of a new alert)


def _ago(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 120:
        return f"{m} min"
    h = m / 60
    return f"{h:.1f} h" if h < 48 else f"{h / 24:.1f} days"


def _what(mig: dict[str, Any]) -> str:
    return f"{'replica' if mig['mode'] == 'replica' else 'migration'} {mig['name']}"


def evaluate(repo: Any, now: float | None = None) -> list[Alert]:
    now = now or time.time()
    out: list[Alert] = []
    for mig in repo.list_migrations(None):
        base = {"project": mig["project"], "migration_id": mig["id"], "object_name": mig["name"]}
        execs = [x for x in repo.list_executions(mig["id"]) if not x.get("dry_run")]
        latest: dict[str, dict[str, Any]] = {}
        for x in execs:                                   # newest first
            g = KIND_GROUP.get(x["kind"])
            if g and g not in latest and x["status"] in FINISHED:
                latest[g] = x
        for g, x in latest.items():
            if x["status"] == "failed":
                out.append(Alert(key=f"failed:{g}:{mig['id']}", kind=f"{g}_failed", severity="critical",
                                 title=f"{_what(mig)}: {GROUP_TITLE[g]}",
                                 detail=(x.get("error") or "no error recorded")[:2000],
                                 execution_id=x["id"], since=x.get("finished_at"), **base))
        if mig["mode"] != "replica":
            continue
        st = mig.get("state") or {}
        rpo = float((mig.get("options") or {}).get("rpo_minutes") or 0)
        if rpo > 0:
            done = [float(s.get("finished") or 0) for s in st.get("syncs") or [] if s.get("ok", True)]
            last = max(done) if done else None
            ref = last if last is not None else float(mig["created_at"])
            if now - ref > rpo * 60:
                detail = (f"newest recovery point is {_ago(now - last)} old; the objective is {_ago(rpo * 60)}"
                          if last is not None else f"no sync has completed since the replica was created "
                                                   f"{_ago(now - ref)} ago; the objective is {_ago(rpo * 60)}")
                out.append(Alert(key=f"rpo:{mig['id']}", kind="rpo_breached", severity="critical",
                                 title=f"{_what(mig)}: recovery point objective missed", detail=detail,
                                 since=ref + rpo * 60, **base))
        h = st.get("helper") or {}
        if h.get("last_reap_error"):
            out.append(Alert(key=f"helper:{mig['id']}", kind="helper_cleanup_failed", severity="warning",
                             title=f"{_what(mig)}: kept helper VM could not be removed",
                             detail=f"{h.get('name')}: {h['last_reap_error']}"[:2000], **base))
    by_id: dict[str, dict[str, Any]] = {}
    for x in repo.list_executions(status="queued"):
        reason = x.get("waiting_reason")
        if not reason or now - float(x["created_at"]) < WAIT_MINUTES * 60:
            continue
        mig = by_id.get(x["migration_id"]) or repo.get_migration(x["migration_id"]) or {}
        by_id[x["migration_id"]] = mig
        out.append(Alert(key=f"waiting:{x['id']}", kind="no_worker", severity="warning",
                         title=f"{_what(mig) if mig else 'execution'}: waiting for a worker",
                         detail=f"queued {_ago(now - float(x['created_at']))} ago: {reason}",
                         project=mig.get("project"), migration_id=x["migration_id"], execution_id=x["id"],
                         object_name=mig.get("name")))
    return out
