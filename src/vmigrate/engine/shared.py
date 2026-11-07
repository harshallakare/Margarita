"""Purpose: coordinate cluster SHARED disks across the migrations of one cluster: exactly one
migration (the owner) creates, copies and verifies a shared disk; every other member attaches the
same target disk.

Usage:
    broker = MemorySharedDisks()      # CLI / tests;  db.repo.DbSharedDisks(repo) on the platform
    row = broker.claim("sql-cluster", "naa.600...", "mig-1")   # first caller becomes owner
    broker.set_target(..., ref_dict); broker.done(...)          # owner
    broker.get("sql-cluster", "naa.600...")                     # members poll

Status: claimed -> created (target disk exists) -> done (copied + verified) | failed.
Deadlock-free by construction: an owner never waits for anything; a member waits for creation in
deploy_disks (owners create all their disks there, before copying) and for completion only in
its replicate_disk tasks, which are scheduled after its own disks.
"""

from __future__ import annotations

import threading
import time
from typing import Any


class MemorySharedDisks:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

    def claim(self, cluster: str, shared_id: str, migration: str, size: int) -> dict[str, Any]:
        with self._lock:
            row = self._rows.setdefault((cluster, shared_id), {"cluster": cluster, "shared_id": shared_id,
                                                               "owner": migration, "status": "claimed",
                                                               "target": None, "size_bytes": size,
                                                               "updated_at": time.time()})
            return dict(row)

    def get(self, cluster: str, shared_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._rows.get((cluster, shared_id))
            return dict(r) if r else None

    def update(self, cluster: str, shared_id: str, owner: str, **vals: Any) -> None:
        with self._lock:
            r = self._rows[(cluster, shared_id)]
            if r["owner"] != owner:
                raise PermissionError(f"shared disk {shared_id} belongs to {r['owner']}")
            r.update(vals, updated_at=time.time())
