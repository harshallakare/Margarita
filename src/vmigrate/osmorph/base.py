"""Purpose: the OSMorphing strategy registry, keyed by (guest OS family, target platform family).

Usage:
    strategy = resolve(OsFamily.LINUX, "kvm")
    result = strategy.run(MorphRequest(...), ctx)

A step is `required` when the guest will not boot without it (e.g. missing virtio drivers) and
`optional` when it is hardening (fstab nofail). Policy:
  * required step that cannot run here -> status "manual_required" (engine stops before
    creating the target VM unless osmorph.allow_manual is set, and then reports it loudly)
  * optional step that cannot run here -> skipped, listed as a NOTE in the report
Unregistered (family, platform) cells resolve to ManualStrategy — never a silent no-op.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.context import RunContext
from ..ir import DiskBus, OsFamily, VmSpec


@dataclass
class MorphRequest:
    spec: VmSpec
    target_platform: str            # "kvm" | "vmware" | "aws" | "azure"
    target_boot_bus: DiskBus
    images: list[tuple[Path, str]]  # (committed image path, format) in disk order; may be empty
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class MorphStep:
    name: str
    description: str
    required: bool
    run: Callable[[], str] | None = None   # None => cannot be automated here


@dataclass
class MorphResult:
    status: str                     # not_needed | applied | planned | manual_required
    strategy: str
    steps: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "strategy": self.strategy, "steps": self.steps, "notes": self.notes}


class MorphStrategy(ABC):
    name = "base"

    @abstractmethod
    def plan(self, req: MorphRequest) -> list[MorphStep]: ...

    def run(self, req: MorphRequest, ctx: RunContext) -> MorphResult:
        steps = self.plan(req)
        res = MorphResult(status="not_needed", strategy=self.name)
        if not steps:
            res.steps.append("no changes required")
            return res
        if ctx.dry_run:
            res.status = "planned"
            for s in steps:
                tag = "REQUIRED" if s.required else "optional"
                auto = "auto" if s.run else "MANUAL"
                res.steps.append(f"[{tag}/{auto}] {s.name}: {s.description}")
            if any(s.required and s.run is None for s in steps):
                res.status = "manual_required"
            return res
        manual_required = False
        applied = False
        for s in steps:
            if s.run is None:
                if s.required:
                    manual_required = True
                    res.notes.append(f"MANUAL OSMORPH REQUIRED: {s.name}: {s.description}")
                else:
                    res.notes.append(f"skipped (cannot automate here, do manually): {s.name}: {s.description}")
                continue
            ctx.require_execute("osmorph step", step=s.name)
            out = s.run()
            res.steps.append(f"{s.name}: {out}")
            applied = True
        res.status = "manual_required" if manual_required else ("applied" if applied else "not_needed")
        return res


class ManualStrategy(MorphStrategy):
    name = "manual"

    def __init__(self, reason: str):
        self.reason = reason

    def plan(self, req: MorphRequest) -> list[MorphStep]:
        return [MorphStep("manual-osmorph", self.reason, required=True, run=None)]


_REGISTRY: dict[tuple[OsFamily, str], Callable[[], MorphStrategy]] = {}


def register(family: OsFamily, platform: str, factory: Callable[[], MorphStrategy]) -> None:
    _REGISTRY[(family, platform)] = factory


def resolve(family: OsFamily, platform: str) -> MorphStrategy:
    f = _REGISTRY.get((family, platform))
    if f is None:
        return ManualStrategy(
            f"no automatic OSMorph strategy for guest family {family.value!r} -> target platform {platform!r}"
        )
    return f()


def registered() -> list[tuple[str, str]]:
    return sorted((f.value, p) for f, p in _REGISTRY)
