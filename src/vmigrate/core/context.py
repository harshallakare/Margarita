"""Purpose: RunContext — carries run id, dry-run flag and logger into every provider, and is the
single choke point that makes destructive calls unreachable in dry-run.

Usage (inside any provider method that changes anything, on either side):
    def create_disk(self, ...):
        self.ctx.require_execute("create cinder volume", name=name, size=size)
        ...

Defense in depth: the engine itself never calls mutating provider methods in dry-run (it only
builds a plan), AND every mutating provider method calls require_execute(), which raises
DryRunViolation when dry_run is True.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from .errors import DryRunViolation


@dataclass
class PlannedAction:
    action: str
    details: dict[str, Any]


@dataclass
class RunContext:
    dry_run: bool = True
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("vmigrate"))
    planned: list[PlannedAction] = field(default_factory=list)

    def require_execute(self, action: str, **details: Any) -> None:
        if self.dry_run:
            raise DryRunViolation(f"refusing {action!r} in dry-run mode ({details}); pass --execute")
        self.logger.info("ACTION %s %s", action, details)

    def plan(self, action: str, **details: Any) -> None:
        """Record what WOULD happen (dry-run) — never does anything."""
        self.planned.append(PlannedAction(action, details))
