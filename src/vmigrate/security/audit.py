"""Purpose: audit trail helpers — secret redaction, naming an API call as an audit action, and the
tamper-evident hash of one audit row.

Usage:
    redact({"password": "x", "config": {"host": "h"}})   # -> {"password": "***", "config": {"host": "h"}}
    describe("POST", "/replicas/{rid}/cutover")          # -> ("replica.cutover", "replica")
    row_hash(prev_hash, row)                              # chain link stored with the row

What is audited: every API call that can change state (POST/PUT/PATCH/DELETE, including failed and
denied ones and logins), the few GETs that hand out data in bulk (AUDITED_GETS), and actions the
conductor takes on its own (actor "system:conductor"). Request bodies are kept with every value whose
key looks secret replaced by "***", and capped in size. Rows are never updated or deleted through
the API; each stores sha256(prev_hash + canonical row), so editing or removing a row in the database
breaks the chain that GET /audit/verify checks.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

SECRET_KEY = re.compile(r"pass|secret|token|credential|private|jwt|cookie|^key$|_key$|authorization", re.I)
MASK = "***"
MAX_DETAILS = 8192
AUDITED_GETS = {"/executions/{xid}/diagnostics", "/audit/export"}
COLLECTIONS = {"users": "user", "endpoints": "endpoint", "migrations": "migration", "replicas": "replica",
               "executions": "execution", "schedules": "schedule", "pools": "pool", "minions": "minion",
               "workers": "worker", "secrets": "secret", "audit": "audit"}
SPECIAL = {("POST", "/auth/token"): ("auth.login", "user"), ("POST", "/auth/logout"): ("auth.logout", "user"),
           ("POST", "/enroll"): ("worker.enroll", "worker")}
VERB = {"POST": "create", "PUT": "update", "PATCH": "update", "DELETE": "delete", "GET": "read"}


def redact(o: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "…"
    if isinstance(o, dict):
        return {k: (MASK if SECRET_KEY.search(str(k)) and v not in (None, "", [], {}) else redact(v, depth + 1))
                for k, v in o.items()}
    if isinstance(o, list):
        return [redact(x, depth + 1) for x in o[:50]]
    if isinstance(o, str) and len(o) > 2000:
        return o[:2000] + "…"
    return o


def clip(details: dict[str, Any]) -> dict[str, Any]:
    s = json.dumps(details, default=str)
    if len(s) <= MAX_DETAILS:
        return details
    return {"truncated": True, "preview": s[:MAX_DETAILS]}


def describe(method: str, route: str) -> tuple[str, str]:
    """('<object>.<verb>', '<object>') for an API route template like /replicas/{rid}/cutover."""
    if (method, route) in SPECIAL:
        return SPECIAL[(method, route)]
    segs = [s for s in route.split("/") if s]
    if not segs:
        return f"api.{VERB.get(method, method.lower())}", "api"
    nouns = [(i, s) for i, s in enumerate(segs) if not s.startswith("{") and s in COLLECTIONS]
    obj = COLLECTIONS[nouns[-1][1]] if nouns else segs[0].rstrip("s")
    last = segs[-1]
    if last.startswith("{") or last in COLLECTIONS:
        verb = VERB.get(method, method.lower())
    else:
        verb = last
    return f"{obj}.{verb}", obj


def outcome(status: int) -> str:
    if status < 400:
        return "success"
    return "denied" if status in (401, 403) else "failed"


HASHED = ("ts", "actor", "actor_role", "ip", "action", "object_type", "object_id", "object_name", "outcome",
          "status", "details")


def row_hash(prev: str, row: dict[str, Any]) -> str:
    canon = json.dumps({k: row.get(k) for k in HASHED}, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256((prev + "\n" + canon).encode()).hexdigest()
