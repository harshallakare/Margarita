"""Purpose: first-class safety gates. The disk-count gate is the reason this project exists
(Coriolis's Nutanix provider saw 10 of RIS-Sainath's 13 disks and silently dropped SCSI >= 10).

Usage:
    check_disk_counts(spec, source.disk_count_crosscheck(vm))     # raises DiskCountMismatch
    check_target_attach(spec, target.list_attached_disks(vm))     # raises DiskCountMismatch
    require_snapshot(snapshot_ref)                                 # raises LiveDiskReadRefused

Gates never auto-retry and never degrade to a warning.
"""

from __future__ import annotations

from ..core.errors import DiskCountMismatch, LiveDiskReadRefused, SafetyGateError
from ..core.interfaces import SnapshotRef
from ..ir import VmSpec

#: Coriolis's cap. Any VM at or above it gets an explicit log line proving we saw them all.
LEGACY_DISK_CAP = 10


def check_disk_counts(spec: VmSpec, crosscheck: dict[str, int]) -> dict[str, int]:
    if len(crosscheck) < 2:
        raise SafetyGateError(
            f"{spec.name}: source provider returned {len(crosscheck)} disk enumeration(s); "
            "at least 2 independent methods are required"
        )
    counts = {"ir.inspect_vm": len(spec.disks), **crosscheck}
    if len(set(counts.values())) != 1:
        raise DiskCountMismatch(spec.name, counts)
    return counts


def check_snapshot_disks(spec: VmSpec, visible: int | None) -> None:
    if visible is None:
        return
    if visible != len(spec.disks):
        raise DiskCountMismatch(spec.name, {"ir.inspect_vm": len(spec.disks), "snapshot.materialized": visible})


def check_target_attach(spec: VmSpec, attached_ids: list[str]) -> None:
    if len(attached_ids) != len(spec.disks):
        raise DiskCountMismatch(spec.name, {"ir.inspect_vm": len(spec.disks), "target.attached": len(attached_ids)})


def require_snapshot(snapshot: SnapshotRef | None) -> SnapshotRef:
    if snapshot is None or not snapshot.id:
        raise LiveDiskReadRefused("refusing to read disks without a snapshot/checkpoint")
    return snapshot
