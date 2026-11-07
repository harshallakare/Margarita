"""Purpose: persisted, resumable per-VM / per-disk migration state (one JSON file per migration).

Usage:
    store = StateStore(state_dir)
    st = store.load_or_new("ris-sainath", source="ahv1", target="vhi1")
    st.phase = Phase.SNAPSHOTTED; store.save(st)

Writes are atomic (tmp + fsync + rename) and guarded by a lock so parallel disk workers can
update watermarks safely. The `created` map records every target/source artefact this tool made,
which is the ONLY input cleanup is allowed to delete from.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class Phase(StrEnum):
    NEW = "new"
    INSPECTED = "inspected"
    SNAPSHOTTED = "snapshotted"
    DISKS_CREATED = "disks_created"
    DISKS_COPIED = "disks_copied"        # every disk copied, verified and committed on the target
    MORPHED = "morphed"
    VM_CREATED = "vm_created"
    DISKS_ATTACHED = "disks_attached"
    VERIFIED = "verified"
    DONE = "done"
    FAILED = "failed"


PHASE_ORDER = list(Phase)


def phase_at_least(cur: Phase, wanted: Phase) -> bool:
    if cur == Phase.FAILED:
        return False
    return PHASE_ORDER.index(cur) >= PHASE_ORDER.index(wanted)


@dataclass
class DiskState:
    disk_id: str
    order: int
    size_bytes: int
    target: dict[str, Any] | None = None  # TargetDiskRef as dict
    bytes_copied: int = 0              # durable resume watermark (only advanced after flush)
    progress_bytes: int = 0            # live progress for the UI (NOT a resume point)
    rate_mb_s: float = 0.0
    progress_at: float | None = None
    copied: bool = False
    committed: bool = False
    attached: bool = False
    staged_path: str | None = None
    copy_stats: dict[str, Any] | None = None
    verify: dict[str, Any] | None = None


@dataclass
class MigrationState:
    name: str
    source: str
    target: str
    phase: Phase = Phase.NEW
    last_good_phase: Phase = Phase.NEW
    spec: dict[str, Any] | None = None
    crosscheck: dict[str, int] | None = None
    snapshot: dict[str, Any] | None = None
    target_vm: dict[str, Any] | None = None
    disks: dict[str, DiskState] = field(default_factory=dict)
    morph: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    created: dict[str, Any] = field(default_factory=lambda: {"target_disks": [], "target_vm": None, "source": []})
    errors: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)

    def set_phase(self, p: Phase) -> None:
        self.phase = p
        if p != Phase.FAILED:
            self.last_good_phase = p
        self.history.append({"t": time.time(), "phase": p.value})

    def resume_phase(self) -> Phase:
        return self.last_good_phase if self.phase == Phase.FAILED else self.phase

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["phase"] = self.phase.value
        d["last_good_phase"] = self.last_good_phase.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MigrationState:
        d = dict(d)
        d["phase"] = Phase(d["phase"])
        d["last_good_phase"] = Phase(d.get("last_good_phase", d["phase"]))
        d["disks"] = {k: DiskState(**v) for k, v in d.get("disks", {}).items()}
        return cls(**d)


class StateStore:
    def __init__(self, state_dir: str | os.PathLike[str]):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def path(self, name: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
        return self.dir / f"{safe}.state.json"

    def load(self, name: str) -> MigrationState | None:
        p = self.path(name)
        if not p.exists():
            return None
        return MigrationState.from_dict(json.loads(p.read_text()))

    def load_or_new(self, name: str, *, source: str, target: str) -> MigrationState:
        st = self.load(name)
        if st is None:
            return MigrationState(name=name, source=source, target=target)
        if (st.source, st.target) != (source, target):
            raise ValueError(
                f"state for {name!r} belongs to {st.source}->{st.target}, not {source}->{target}"
            )
        return st

    def save(self, st: MigrationState) -> None:
        with self._lock:
            p = self.path(st.name)
            tmp = p.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(st.to_dict(), f, indent=2, sort_keys=True, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)

    def all(self) -> list[MigrationState]:
        return [MigrationState.from_dict(json.loads(p.read_text())) for p in sorted(self.dir.glob("*.state.json"))]
