"""Purpose: Replica mode (incremental DR, "DRaaS"). A replica keeps one set of REPLICA DISKS on the
target that every sync brings up to date; Deploy (failover / test failover) clones them into a new
bootable VM, so the replica keeps syncing afterwards. The source is only ever snapshotted and read.

Usage:
    rr = ReplicaRunner(job, engine, source, target, ctx, store, retention=3)
    DagRunner(task_store, rr.sync_handlers()).run()          # specs: sync_dag(dry_run)
    DagRunner(task_store, rr.deploy_handlers(params)).run()  # specs: deploy_dag(dry_run)
    DagRunner(task_store, rr.delete_handlers()).run()        # specs: delete_dag(dry_run)

Sync (per execution):
  validate_endpoints -> inspect_instance (DISK-COUNT GATE; disk set/sizes must not have changed)
  -> create_snapshot (+ snapshot disk-count gate; an unfinished sync is RESUMED with its snapshot)
  -> deploy_replica_disks (created once) ==expands==> replicate_disk:<id> x N
  -> finalize_sync (restore point if the target supports it, retention, source snapshot rotation)
  replicate_disk picks the cheapest correct mode per disk:
    full     first sync of the disk: everything is read and written
    cbt      source change tracking: read + write ONLY changed regions since the last sync
    compare  no change tracking: read everything, write only chunks that differ from the replica
Deploy: validate_endpoints (target) -> check_replica -> clone_disks -> osmorphing -> deploy_instance
        -> verify_instance -> finalize_deploy. Deploy never contacts the source.
Cutover (planned failover, one execution): validate_endpoints -> inspect_instance -> check_cutover
        -> shutdown_source (ONLY with shutdown_source=true; otherwise recorded as skipped and the final
        sync is crash-consistent at its snapshot) -> the sync tasks (final, normally small, delta)
        -> clone_disks -> osmorphing -> deploy_instance -> verify_instance -> finalize_deploy
        -> finalize_cutover. The source is never deleted; it is left powered off when it was shut down.
Consistency: replica disks are mid-update while a sync runs; `consistent` is False from the first
write until finalize_sync. Deploying the latest state requires consistent=True; restore points
(frozen copies) can be deployed at any time.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from ..config.loader import EngineConfig, MigrationConfig
from ..core.context import RunContext
from ..core.errors import ChangeTrackingUnavailable, SafetyGateError, VerificationError
from ..core.fileio import RawFileReader
from ..core.interfaces import CheckpointRef, DiskHints, SnapshotRef, SourceProvider, TargetDiskRef, TargetProvider, VmRef
from ..ir import DiskSpec, GuestOS, OsFamily, VmSpec
from . import safety
from .copier import HASH_BLOCK, BlockHashes, RateMeter, align_ranges, copy_disk
from .dag import Handler, TaskContext, TaskOutcome, TaskSpec
from .pipeline import MigrationRunner, _ref_to_dict, _snap, _tdisk
from .state import DiskState, MigrationState, Phase
from .verify import check_size, compare_samples, full_verify, sample_offsets


def _gib(n: int) -> str:
    for unit, shift in (("TiB", 40), ("GiB", 30), ("MiB", 20)):
        if n >= 1 << shift:
            return f"{n / (1 << shift):.1f} {unit}"
    return f"{n} B"


# --------------------------------------------------------------------------- graphs

def sync_dag(dry_run: bool) -> list[TaskSpec]:
    t = [TaskSpec("validate_endpoints", "validate_endpoints", label="Validate endpoints"),
         TaskSpec("inspect_instance", "inspect_instance", ["validate_endpoints"], label="Inspect VM + disk-count gate")]
    if dry_run:
        return t + [TaskSpec("plan", "plan", ["inspect_instance"], label="Build dry-run plan")]
    return t + [
        TaskSpec("create_snapshot", "create_snapshot", ["inspect_instance"], label="Snapshot source"),
        TaskSpec("deploy_replica_disks", "deploy_replica_disks", ["create_snapshot"], label="Replica disks"),
        TaskSpec("finalize_sync", "finalize_sync", ["deploy_replica_disks"], label="Finalize sync (restore point, retention)"),
    ]


def deploy_dag(dry_run: bool) -> list[TaskSpec]:
    t = [TaskSpec("validate_endpoints", "validate_target", label="Validate target"),
         TaskSpec("check_replica", "check_replica", ["validate_endpoints"], label="Check replica state")]
    if dry_run:
        return t + [TaskSpec("plan", "plan_deploy", ["check_replica"], label="Build dry-run plan")]
    return t + [
        TaskSpec("clone_disks", "clone_disks", ["check_replica"], label="Clone replica disks"),
        TaskSpec("osmorphing", "osmorphing", ["clone_disks"], label="OSMorphing"),
        TaskSpec("deploy_instance", "deploy_instance", ["osmorphing"], label="Create VM + attach disks"),
        TaskSpec("verify_instance", "verify_instance", ["deploy_instance"], label="Verify (counts, boot)"),
        TaskSpec("finalize_deploy", "finalize_deploy", ["verify_instance"], label="Finalize deploy"),
    ]


def cutover_dag(dry_run: bool) -> list[TaskSpec]:
    t = [TaskSpec("validate_endpoints", "validate_endpoints", label="Validate endpoints"),
         TaskSpec("inspect_instance", "inspect_instance", ["validate_endpoints"], label="Inspect VM + disk-count gate"),
         TaskSpec("check_cutover", "check_cutover", ["inspect_instance"], label="Check replica (initial sync done)")]
    if dry_run:
        return t + [TaskSpec("plan", "plan_cutover", ["check_cutover"], label="Build dry-run plan")]
    return t + [
        TaskSpec("shutdown_source", "shutdown_source", ["check_cutover"], label="Shut down source (opt-in)"),
        TaskSpec("create_snapshot", "create_snapshot", ["shutdown_source"], label="Final sync: snapshot source"),
        TaskSpec("deploy_replica_disks", "deploy_replica_disks", ["create_snapshot"], label="Final sync: replica disks"),
        TaskSpec("finalize_sync", "finalize_sync", ["deploy_replica_disks"], label="Final sync: finalize"),
        TaskSpec("clone_disks", "clone_disks", ["finalize_sync"], label="Clone replica disks"),
        TaskSpec("osmorphing", "osmorphing", ["clone_disks"], label="OSMorphing"),
        TaskSpec("deploy_instance", "deploy_instance", ["osmorphing"], label="Create VM + attach disks"),
        TaskSpec("verify_instance", "verify_instance", ["deploy_instance"], label="Verify (counts, boot)"),
        TaskSpec("finalize_deploy", "finalize_deploy", ["verify_instance"], label="Finalize deploy"),
        TaskSpec("finalize_cutover", "finalize_cutover", ["finalize_deploy"], label="Finalize cutover"),
    ]


def delete_dag(dry_run: bool) -> list[TaskSpec]:
    t = [TaskSpec("validate_endpoints", "validate_target", label="Validate target")]
    if dry_run:
        return t + [TaskSpec("plan", "plan_delete", ["validate_endpoints"], label="List what would be deleted")]
    return t + [TaskSpec("delete_replica_disks", "delete_replica_disks", ["validate_endpoints"],
                         label="Delete replica disks + restore points + source base snapshot")]


# --------------------------------------------------------------------------- state

@dataclass
class ReplicaState:
    name: str
    source: str
    target: str
    spec: dict[str, Any] | None = None
    crosscheck: dict[str, int] | None = None
    disks: dict[str, dict[str, Any]] = field(default_factory=dict)  # disk_id -> {target, order, size_bytes, synced}
    base_checkpoint: dict[str, Any] | None = None   # source CheckpointRef of the last completed sync
    base_snapshot: dict[str, Any] | None = None     # source SnapshotRef kept as the CBT diff base
    current: dict[str, Any] | None = None           # sync in progress (resumable)
    consistent: bool = False                        # replica disks == the last completed sync
    syncs: list[dict[str, Any]] = field(default_factory=list)
    restore_points: list[dict[str, Any]] = field(default_factory=list)
    deployments: list[dict[str, Any]] = field(default_factory=list)
    cutovers: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "replica", **asdict(self)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ReplicaState:
        d = {k: v for k, v in d.items() if k != "kind"}
        return cls(**d)


class ReplicaStore:
    """Persistence seam: load() -> dict | None, save(dict). DB: migrations.state (see db.repo)."""

    def load(self) -> dict[str, Any] | None:
        raise NotImplementedError

    def save(self, d: dict[str, Any]) -> None:
        raise NotImplementedError

    # Block checksums of each replica disk (kept apart from the state: ~2 MB per 250 GiB disk).
    # A store without them simply disables checksum compare (target-read compare is used).
    def load_hashes(self, disk_id: str) -> tuple[int, bytes] | None:
        return None

    def save_hashes(self, disk_id: str, block_size: int, data: bytes | None) -> None:
        pass


class MemoryReplicaStore(ReplicaStore):
    def __init__(self) -> None:
        self.data: dict[str, Any] | None = None

    def load(self) -> dict[str, Any] | None:
        return self.data

    def save(self, d: dict[str, Any]) -> None:
        import json
        self.data = json.loads(json.dumps(d, default=str))

    hashes: dict[str, tuple[int, bytes]]

    def load_hashes(self, disk_id: str) -> tuple[int, bytes] | None:
        return getattr(self, "hashes", {}).get(disk_id)

    def save_hashes(self, disk_id: str, block_size: int, data: bytes | None) -> None:
        h = self.__dict__.setdefault("hashes", {})
        if data is None:
            h.pop(disk_id, None)
        else:
            h[disk_id] = (block_size, bytes(data))


class _DeployStateStore:
    """Lets MigrationRunner persist a deployment's MigrationState inside the replica state."""

    def __init__(self, rr: ReplicaRunner, dep_id: str):
        self.rr, self.dep_id = rr, dep_id

    def _dep(self) -> dict[str, Any]:
        return next(d for d in self.rr.state.deployments if d["id"] == self.dep_id)

    def load_or_new(self, name: str, *, source: str, target: str) -> MigrationState:
        st = self._dep().get("state")
        return MigrationState.from_dict(st) if st else MigrationState(name=name, source=source, target=target)

    def save(self, st: MigrationState) -> None:
        with self.rr._lock:
            self._dep()["state"] = st.to_dict()
            self.rr._save()


# --------------------------------------------------------------------------- runner

class ReplicaRunner:
    def __init__(self, job: MigrationConfig, engine: EngineConfig, source: SourceProvider, target: TargetProvider,
                 ctx: RunContext, store: ReplicaStore, *, retention: int = 3):
        self.job, self.engine, self.source, self.target, self.ctx, self.store = job, engine, source, target, ctx, store
        self.retention = max(1, int(retention))
        self.log = ctx.logger.getChild(job.name) if isinstance(ctx.logger, logging.Logger) else ctx.logger
        self._lock = threading.RLock()
        raw = store.load()
        self.state = ReplicaState.from_dict(raw) if raw else ReplicaState(job.name, job.source, job.target)
        if (self.state.source, self.state.target) != (job.source, job.target):
            raise SafetyGateError(f"replica {job.name}: state belongs to {self.state.source}->{self.state.target}")
        self._disk_ctx: dict[str, TaskContext] = {}

    def _save(self) -> None:
        with self._lock:
            self.store.save(self.state.to_dict())

    def spec(self) -> VmSpec:
        if not self.state.spec:
            raise SafetyGateError("replica has no inspected VM spec yet")
        return VmSpec.from_dict(self.state.spec)

    def _disk_name(self, d: DiskSpec) -> str:
        return f"{self.job.name}-r{d.order:02d}"

    def _mode_for(self, d: DiskSpec) -> str:
        if not self.state.disks.get(d.id, {}).get("synced"):
            return "full"
        if self.source.supports_cbt() and self.state.base_checkpoint:
            return "cbt"
        return "compare"

    # ------------------------------------------------------------------ shared tasks
    def _validate_source(self, ctx: TaskContext) -> TaskOutcome:
        vm = self.source.find_vm(self.job.vm)
        problems = self.source.preflight(None) + self.target.preflight(None)
        if not self.target.supports_replica():
            problems.append(f"target type {self.target.type_name!r} does not support replicas yet")
        if problems:
            raise SafetyGateError("endpoint validation failed:\n  - " + "\n  - ".join(problems))
        return TaskOutcome(result={"source_vm": asdict(vm)}, message=f"found {vm.name}")

    def _validate_target(self, ctx: TaskContext) -> TaskOutcome:
        problems = self.target.preflight(None)
        if problems:
            raise SafetyGateError("target validation failed:\n  - " + "\n  - ".join(problems))
        return TaskOutcome(message="target reachable")

    def _inspect(self, ctx: TaskContext) -> TaskOutcome:
        vm = self.source.find_vm(self.job.vm)
        spec = self.source.inspect_vm(vm)
        if self.job.guest_os:
            g = self.job.guest_os
            spec.guest_os = GuestOS(family=OsFamily(g.get("family", spec.guest_os.family)),
                                    distro=g.get("distro", spec.guest_os.distro),
                                    version=g.get("version", spec.guest_os.version), bits=int(g.get("bits", 64)))
        spec.extra["target_options"] = dict(self.job.target_options)
        spec.extra["max_parallel_disks"] = self.engine.max_parallel_disks
        spec.validate()
        counts = safety.check_disk_counts(spec, self.source.disk_count_crosscheck(vm))
        if self.state.spec is not None:
            before = {d["id"]: d["size_bytes"] for d in self.state.spec["disks"]}
            now = {d.id: d.size_bytes for d in spec.disks}
            if set(before) != set(now):
                raise SafetyGateError(f"{spec.name}: disk set changed since the replica was created "
                                      f"({sorted(before)} -> {sorted(now)}); create a new replica")
            grown = [k for k in now if now[k] != before[k]]
            if grown:
                raise SafetyGateError(f"{spec.name}: disk size changed for {grown}; resizing replicas is not supported yet")
        problems = self.source.preflight(spec) + self.target.preflight(spec)
        if problems:
            raise SafetyGateError("preflight failed:\n  - " + "\n  - ".join(problems))
        with self._lock:
            self.state.spec = spec.to_dict()
            self.state.crosscheck = counts
            for d in spec.disks:
                self.state.disks.setdefault(d.id, {"target": None, "order": d.order, "size_bytes": d.size_bytes,
                                                   "synced": False})
        self._save()
        return TaskOutcome(result={"vm": asdict(vm), "disks": len(spec.disks), "crosscheck": counts},
                           message=f"{len(spec.disks)} disks, {_gib(spec.total_disk_bytes())}; counts agree: {counts}")

    # ------------------------------------------------------------------ sync
    def _plan_sync(self, ctx: TaskContext) -> TaskOutcome:
        s = self.spec()
        steps = [{"action": "source.create_snapshot", "vm": s.name}]
        for d in s.ordered_disks():
            exists = bool(self.state.disks.get(d.id, {}).get("target"))
            if not exists:
                steps.append({"action": "target.create_disk", "name": self._disk_name(d), "size_bytes": d.size_bytes,
                              "source": d.address})
            steps.append({"action": "replicate_disk", "disk": d.address, "mode": self._mode_for(d),
                          "size_bytes": d.size_bytes})
        ok, why = self.target.supports_restore_points()
        steps.append({"action": "target.restore_point" if ok else "no_restore_point", "retention": self.retention,
                      "note": "" if ok else why})
        steps.append({"action": "source.rotate_snapshot", "keep_as_cbt_base": self.source.keep_for_cbt()})
        return TaskOutcome(result={"plan": steps}, message=f"{len(steps)} planned actions (nothing changed)")

    def _create_snapshot(self, ctx: TaskContext) -> TaskOutcome:
        s = self.spec()
        cur = self.state.current
        if cur and cur.get("snapshot"):
            snap = _snap(cur["snapshot"])
            self.log.info("resuming unfinished sync %s with snapshot %s", cur["id"], snap.id)
        else:
            app = self.job.app_consistent if self.job.app_consistent is not None else self.engine.app_consistent
            snap = self.source.create_snapshot(self.source.find_vm(self.job.vm), app_consistent=app)
            with self._lock:
                self.state.current = {"id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:4],
                                      "started": time.time(), "snapshot": _ref_to_dict(snap), "disks": {},
                                      "checkpoint": _ref_to_dict(self.source.checkpoint(snap))}
            self._save()
        safety.check_snapshot_disks(s, self.source.snapshot_disk_count(snap))
        safety.require_snapshot(snap)
        return TaskOutcome(result={"snapshot_id": snap.id, "sync_id": self.state.current["id"]},  # type: ignore[index]
                           message=f"snapshot {snap.id}")

    def _deploy_replica_disks(self, ctx: TaskContext) -> TaskOutcome:
        s = self.spec()
        for d in s.ordered_disks():
            ds = self.state.disks[d.id]
            if ds.get("target"):
                continue
            name = self._disk_name(d)
            ref = self.target.find_disk(name) or self.target.create_disk(
                name, d.size_bytes, DiskHints(bus=d.bus, is_boot=d.is_boot, order=d.order,
                                              storage_pool=self.job.target_options.get("storage_pool")))
            ok, msg = check_size(d.size_bytes, ref.size_bytes)
            if not ok:
                raise VerificationError(f"{name}: {msg}")
            with self._lock:
                ds["target"] = _ref_to_dict(ref)
            self._save()
        with self._lock:
            self.state.consistent = False   # writes start now
        self._save()
        children = [TaskSpec(f"replicate_disk:{d.id}", "replicate_disk", params={"disk_id": d.id, "size_bytes": d.size_bytes},
                             max_attempts=3,
                             label=f"Disk {d.order:02d} {d.address} ({_gib(d.size_bytes)}) · {self._mode_for(d)}")
                    for d in sorted(s.disks, key=lambda d: -d.size_bytes)]
        return TaskOutcome(children=children, message=f"{len(s.disks)} replica disks ready")

    def _verify(self, snap: SnapshotRef, d: DiskSpec, dst: Any, label: str) -> dict[str, Any]:
        offs = sample_offsets(d.id + label, d.size_bytes, count=self.engine.verify_samples,
                              length=self.engine.verify_sample_bytes)
        with self.source.open_disk(snap, d) as src, dst:
            res = compare_samples(src, dst, offs, length=self.engine.verify_sample_bytes)
        if not res.ok:
            raise VerificationError(f"disk {d.id}: {label}: {res.detail} at {res.mismatches[:5]}")
        return res.to_dict()

    def _replicate_disk(self, ctx: TaskContext) -> TaskOutcome:
        did = ctx.params["disk_id"]
        s = self.spec()
        d = next(x for x in s.disks if x.id == did)
        ds = self.state.disks[did]
        cur = self.state.current or {}
        if cur.get("disks", {}).get(did, {}).get("done"):
            return TaskOutcome(result=cur["disks"][did], message="already synced in this run")
        snap = _snap(cur["snapshot"])
        ref = _tdisk(ds["target"])
        mode = self._mode_for(d)
        ranges = None
        fallback = None
        if mode == "cbt":
            try:
                ranges = self.source.changed_regions(d, CheckpointRef(**self.state.base_checkpoint),  # type: ignore[arg-type]
                                                     CheckpointRef(**cur["checkpoint"]))
            except ChangeTrackingUnavailable as e:   # e.g. VMware CBT reset: correct but slower
                self.log.warning("disk %s: change tracking unavailable (%s); falling back to compare", d.address, e)
                mode, fallback = "compare", str(e)
        # Block checksums from the previous completed sync. Cleared in the store until THIS sync
        # completes, so an interrupted sync can never leave checksums that disagree with the replica.
        stored = self.store.load_hashes(did)
        base = stored[1] if stored and stored[0] == HASH_BLOCK else None
        hashes = BlockHashes(d.size_bytes, base)
        if stored is not None:
            self.store.save_hashes(did, HASH_BLOCK, None)
        compare_by = None
        hashes_off = False
        meter = RateMeter()

        def progress(off: int, size: int) -> None:
            now = meter.update(off)
            ctx.progress(off * 100 / max(size, 1),
                         f"{compare_by or mode}: {_gib(off)}/{_gib(size)}, {now:.0f} MB/s now · "
                         f"{meter.average(off):.0f} MB/s avg")

        with self.source.open_disk(snap, d) as reader, \
                self.target.open_disk_writer(ref, incremental=mode != "full") as writer:
            if reader.size != d.size_bytes:
                raise SafetyGateError(f"disk {d.id}: snapshot size {reader.size} != inspected size {d.size_bytes}")
            staged = writer.staged_path
            compare = None
            if mode == "compare":
                if hashes.has_base:
                    compare_by = "compare (checksums)"     # source vs stored checksums; replica not read
                else:
                    compare_by = "compare (reading replica)"
                    compare = RawFileReader(staged) if staged else self.target.open_disk_reader(ref)
                    if compare is None:
                        raise SafetyGateError(f"{self.target.type_name}: cannot read the replica back for compare mode")
            if mode == "cbt" and ranges is not None:
                # keeping checksums means re-reading whole 1 MiB blocks around each change; skip that when it
                # would read much more than changed (scattered small writes). Checksums then become unknown,
                # and the next fallback reads the replica once to rebuild them.
                changed = sum(r.length for r in ranges)
                grown = sum(r.length for r in align_ranges(ranges, HASH_BLOCK, d.size_bytes))
                slack = min(64 << 20, d.size_bytes // 50)       # small fixed allowance, at most 2% of the disk
                if not hashes.has_base or grown > changed * 1.5 + slack:
                    hashes = BlockHashes(d.size_bytes)      # unknown: never saved, never trusted
                    hashes_off = True
            if mode == "full" and writer.prezeroed:
                hashes.fill_zero()                       # untouched blocks of a fresh volume are zeros
                ranges = reader.allocated_ranges()   # skip never-written regions when the source knows them
            try:
                stats = copy_disk(reader, writer, chunk_size=self.engine.chunk_size, ranges=ranges,
                                  checkpoint_every=self.engine.checkpoint_every, on_progress=progress, compare=compare,
                                  hashes=None if hashes_off else hashes, prefetch=self.engine.prefetch_chunks)
            finally:
                if compare is not None:
                    compare.close()
        verify: dict[str, Any] = {}
        if staged is not None:
            ctx.progress(100, "copied; verifying samples against the source", force=True)
            verify["staged_samples"] = self._verify(snap, d, RawFileReader(staged), "staged")
        ctx.progress(100, "committing to the replica disk", force=True)
        self.target.commit_disk(ref, staged)
        back = self.target.open_disk_reader(ref)
        if back is not None:
            ctx.progress(100, "verifying the replica disk", force=True)
            verify["target_samples"] = self._verify(snap, d, back, "committed")
        if self.engine.verify_full:
            with self.source.open_disk(snap, d) as src:
                full = full_verify(src, self.target.open_disk_reader(ref), d.size_bytes, chunk=self.engine.chunk_size,
                                   progress=lambda o, n: ctx.progress(100, f"full SHA-256 checksum {o * 100 // max(n, 1)}%"))
            verify["full"] = full
            if not full.get("skipped") and not full["ok"]:
                raise VerificationError(f"disk {d.id}: full checksum mismatch after sync")
        self.target.discard_staging(ref)
        if hashes.complete():                    # only now do they describe the replica as written
            self.store.save_hashes(did, HASH_BLOCK, hashes.to_bytes())
        else:
            self.log.info("disk %s: block checksums incomplete after a %s sync; the next fallback reads the "
                          "replica to rebuild them", d.address, mode)
        rec = {"done": True, "mode": mode, "bytes_read": stats.bytes_read, "bytes_written": stats.bytes_written,
               "compare_by": compare_by, "checksums": hashes.complete(),
               "bytes_unchanged": stats.bytes_unchanged, "bytes_zero_skipped": stats.bytes_zero_skipped,
               "changed_bytes": sum(r.length for r in ranges) if ranges is not None else None,
               "cbt_fallback": fallback,
               "seconds": round(stats.seconds, 3), "verify": {"ok": True, **verify}}
        with self._lock:
            ds["synced"] = True
            self.state.current.setdefault("disks", {})[did] = rec  # type: ignore[union-attr]
        self._save()
        return TaskOutcome(result=rec, message=f"{mode}: read {_gib(stats.bytes_read)}, wrote {_gib(stats.bytes_written)}")

    def _finalize_sync(self, ctx: TaskContext) -> TaskOutcome:
        s = self.spec()
        cur = self.state.current
        if cur is None:
            raise SafetyGateError("no sync in progress")
        if len([v for v in cur.get("disks", {}).values() if v.get("done")]) != len(s.disks):
            raise SafetyGateError("not every disk finished syncing")
        rp = None
        ok, why = self.target.supports_restore_points()
        if ok:
            snaps = {}
            for d in s.ordered_disks():
                snaps[d.id] = self.target.snapshot_disk(_tdisk(self.state.disks[d.id]["target"]), f"rp-{cur['id']}")
            rp = {"id": cur["id"], "at": time.time(), "disks": snaps}
            with self._lock:
                self.state.restore_points.append(rp)
            self._save()
            while len(self.state.restore_points) > self.retention:
                old = self.state.restore_points[0]
                for did, sid in old["disks"].items():
                    self.target.delete_disk_snapshot(_tdisk(self.state.disks[did]["target"]), sid)
                with self._lock:
                    self.state.restore_points.pop(0)
                self._save()
        snap = _snap(cur["snapshot"])
        prev = self.state.base_snapshot
        new_cp: dict[str, Any] = cur["checkpoint"]
        if self.source.keep_for_cbt():
            if prev and prev.get("id") != snap.id:
                self.source.cleanup(_snap(prev))
            new_base: dict[str, Any] | None = cur["snapshot"]
        else:
            old_cp = CheckpointRef(**self.state.base_checkpoint) if self.state.base_checkpoint else None
            new_cp = _ref_to_dict(self.source.retire_snapshot(snap, old_cp))
            new_base = None
        totals = {k: sum((v.get(k) or 0) for v in cur["disks"].values())
                  for k in ("bytes_read", "bytes_written", "bytes_unchanged", "changed_bytes")}
        modes = sorted({v["mode"] for v in cur["disks"].values()})
        rec = {"id": cur["id"], "started": cur["started"], "finished": time.time(), "ok": True, "snapshot_id": snap.id,
               "modes": modes, "restore_point": rp["id"] if rp else None, **totals}
        with self._lock:
            self.state.base_snapshot = new_base
            self.state.base_checkpoint = new_cp
            self.state.syncs.append(rec)
            self.state.syncs = self.state.syncs[-200:]
            self.state.current = None
            self.state.consistent = True
        self._save()
        msg = f"sync {rec['id']}: read {_gib(totals['bytes_read'])}, wrote {_gib(totals['bytes_written'])}"
        return TaskOutcome(result=rec, message=msg + ("" if ok else f" (no restore point: {why})"))

    def sync_handlers(self) -> dict[str, Handler]:
        return {"validate_endpoints": self._validate_source, "inspect_instance": self._inspect, "plan": self._plan_sync,
                "create_snapshot": self._create_snapshot, "deploy_replica_disks": self._deploy_replica_disks,
                "replicate_disk": self._replicate_disk, "finalize_sync": self._finalize_sync}

    # ------------------------------------------------------------------ deploy
    def deploy_handlers(self, params: dict[str, Any]) -> dict[str, Handler]:
        rp_id: str | None = params.get("restore_point")
        name: str = params.get("deploy_name") or f"{self.job.target_name or self.job.vm}-dr"
        dep_id: str = params.get("deployment_id") or uuid.uuid4().hex[:12]
        holder: dict[str, MigrationRunner] = {}

        def check(ctx: TaskContext) -> TaskOutcome:
            s = self.spec()
            if not all(self.state.disks.get(d.id, {}).get("synced") for d in s.disks):
                raise SafetyGateError("the replica has not completed an initial sync of every disk")
            if rp_id:
                if not any(r["id"] == rp_id for r in self.state.restore_points):
                    raise SafetyGateError(f"restore point {rp_id!r} not found (kept: {[r['id'] for r in self.state.restore_points]})")
                src = f"restore point {rp_id}"
            else:
                if not self.state.consistent:
                    raise SafetyGateError("replica disks are mid-sync (last sync unfinished); finish a sync or "
                                          "deploy from a restore point")
                src = f"latest sync {self.state.syncs[-1]['id'] if self.state.syncs else '?'}"
            if self.target.find_vm(name) is not None and not any(
                    d["id"] == dep_id for d in self.state.deployments):
                raise SafetyGateError(f"target already has a VM named {name!r}")
            return TaskOutcome(result={"source": src, "name": name}, message=f"deploy {name} from {src}")

        def plan(ctx: TaskContext) -> TaskOutcome:
            s = self.spec()
            steps = [{"action": "target.clone_disk", "from": self.state.disks[d.id]["target"]["name"],
                      "name": f"{name}-disk{d.order:02d}", "restore_point": rp_id} for d in s.ordered_disks()]
            steps += [{"action": "osmorph"}, {"action": "target.create_vm", "name": name, "disks": len(s.disks)}]
            return TaskOutcome(result={"plan": steps}, message=f"{len(steps)} planned actions (nothing changed)")

        # per-run network override (e.g. boot a test copy on an isolated network to avoid IP conflicts
        # with the still-running source); networks not named keep the replica's mapping
        topts = dict(self.job.target_options)
        if params.get("network_map"):
            topts["network_map"] = {**(topts.get("network_map") or {}), **params["network_map"]}
        if params.get("preserve_mac") is not None:
            topts["preserve_mac"] = bool(params["preserve_mac"])

        def runner() -> MigrationRunner:
            if "r" not in holder:
                job = MigrationConfig(name=name, source=self.job.source, target=self.job.target, vm=self.job.vm,
                                      target_name=name, guest_os=self.job.guest_os,
                                      target_options=topts, osmorph=dict(self.job.osmorph))
                holder["r"] = MigrationRunner(job, self.engine, self.source, self.target, self.ctx,
                                              _DeployStateStore(self, dep_id))  # type: ignore[arg-type]
            return holder["r"]

        def refs(r: MigrationRunner) -> dict[str, TargetDiskRef]:
            return {k: _tdisk(v.target) for k, v in r.state.disks.items()}  # type: ignore[arg-type]

        def clone(ctx: TaskContext) -> TaskOutcome:
            s = self.spec()
            with self._lock:
                if not any(d["id"] == dep_id for d in self.state.deployments):
                    self.state.deployments.append({"id": dep_id, "name": name, "at": time.time(), "restore_point": rp_id,
                                                   "status": "running", "state": None,
                                                   "network_map": params.get("network_map") or None})
            self._save()
            r = runner()
            r.state.spec = {**s.to_dict(), "name": s.name}
            r.state.crosscheck = self.state.crosscheck
            for i, d in enumerate(s.ordered_disks()):
                ds = r.state.disks.get(d.id)
                if ds and ds.target:
                    continue
                rp_snap = None
                if rp_id:
                    rp_snap = next(x for x in self.state.restore_points if x["id"] == rp_id)["disks"][d.id]
                ref = self.target.clone_disk(_tdisk(self.state.disks[d.id]["target"]), f"{name}-disk{d.order:02d}", rp_snap)
                r.state.disks[d.id] = DiskState(disk_id=d.id, order=d.order, size_bytes=d.size_bytes,
                                                target=_ref_to_dict(ref), copied=True, committed=True)
                r.state.created["target_disks"].append(_ref_to_dict(ref))
                r._save()
                ctx.progress((i + 1) * 100 / len(s.disks), f"cloned {i + 1}/{len(s.disks)}")
            if not r._done(Phase.DISKS_COPIED):
                r._phase(Phase.DISKS_COPIED)
            return TaskOutcome(result={"disks": {k: v["id"] for k, v in
                                                 ((k, v.target) for k, v in r.state.disks.items())}},  # type: ignore[misc]
                               message=f"{len(s.disks)} disks cloned")

        def morph(ctx: TaskContext) -> TaskOutcome:
            r = runner()
            r._morph(self.spec(), refs(r))
            m = r.state.morph or {}
            return TaskOutcome(result=m, message=f"{m.get('strategy')}: {m.get('status')}")

        def instance(ctx: TaskContext) -> TaskOutcome:
            r = runner()
            s, rf = self.spec(), refs(r)
            s.extra["target_options"] = dict(topts)          # this run's networks (override or replica's)
            vm = r._create_vm(s, rf)
            if not r._done(Phase.DISKS_ATTACHED):
                r._attach(s, vm, rf)
            return TaskOutcome(result={"target_vm": asdict(vm)}, message=f"VM {vm.name} with {len(rf)} disks")

        def verify(ctx: TaskContext) -> TaskOutcome:
            r = runner()
            if not r._done(Phase.VERIFIED):
                r._verify(self.spec(), VmRef(**r.state.target_vm))  # type: ignore[arg-type]
            return TaskOutcome(result=r.state.verification or {}, message="verified")

        def finalize(ctx: TaskContext) -> TaskOutcome:
            r = runner()
            r._phase(Phase.DONE)
            with self._lock:
                dep = next(d for d in self.state.deployments if d["id"] == dep_id)
                dep["status"] = "done"
                dep["target_vm"] = r.state.target_vm
            self._save()
            return TaskOutcome(message=f"{name} deployed; the replica keeps syncing")

        self._deploy_runner = runner  # for failure handling
        self._deploy_id = dep_id
        return {"validate_target": self._validate_target, "check_replica": check, "plan_deploy": plan,
                "clone_disks": clone, "osmorphing": morph, "deploy_instance": instance, "verify_instance": verify,
                "finalize_deploy": finalize}

    # ------------------------------------------------------------------ cutover
    def cutover_handlers(self, params: dict[str, Any]) -> dict[str, Handler]:
        """params: shutdown_source (bool, default False), force_power_off (bool), shutdown_timeout (s),
        deploy_name, deployment_id, cutover_id."""
        shutdown = bool(params.get("shutdown_source", False))
        force = bool(params.get("force_power_off", False))
        timeout = int(params.get("shutdown_timeout", 900))
        cid: str = params.get("cutover_id") or uuid.uuid4().hex[:12]
        dep = self.deploy_handlers({**params, "restore_point": None})
        name = params.get("deploy_name") or f"{self.job.target_name or self.job.vm}-dr"
        if shutdown:
            # a powered-off guest is already consistent; quiescing (VMware tools, VSS) would fail on it
            self.job.app_consistent = False

        def record(**kw: Any) -> dict[str, Any]:
            with self._lock:
                c = next((x for x in self.state.cutovers if x["id"] == cid), None)
                if c is None:
                    c = {"id": cid, "t": time.time(), "name": name, "status": "running",
                         "shutdown_source": shutdown, "deployment_id": self._deploy_id}
                    self.state.cutovers.append(c)
                c.update(kw)
            self._save()
            return c

        def check(ctx: TaskContext) -> TaskOutcome:
            if not self.state.syncs:
                raise SafetyGateError("cutover needs a completed initial sync (run the replica first, so the "
                                      "final sync during the outage only moves the last changes)")
            if not all(self.state.disks.get(d.id, {}).get("synced") for d in self.spec().disks):
                raise SafetyGateError("not every replica disk has completed a sync")
            if self.target.find_vm(name) is not None and not any(
                    d["id"] == self._deploy_id for d in self.state.deployments):
                raise SafetyGateError(f"target already has a VM named {name!r}")
            record()
            last = self.state.syncs[-1]
            return TaskOutcome(result={"last_sync": last.get("id"), "shutdown_source": shutdown},
                               message=f"cutover to {name}; last sync {last.get('id')}")

        def plan(ctx: TaskContext) -> TaskOutcome:
            s = self.spec()
            steps: list[dict[str, Any]] = [
                {"action": "source.shutdown_vm", "vm": s.name, "force": force} if shutdown else
                {"action": "source_left_running", "note": "final sync is crash-consistent; writes after its "
                                                          "snapshot are not migrated"}]
            steps += self._plan_sync(ctx).result["plan"]  # type: ignore[index]
            steps += dep["plan_deploy"](ctx).result["plan"]  # type: ignore[index]
            return TaskOutcome(result={"plan": steps}, message=f"{len(steps)} planned actions (nothing changed)")

        def stop_source(ctx: TaskContext) -> TaskOutcome:
            if not shutdown:
                record(source="left running")
                return TaskOutcome(message="source left running (shutdown_source not set): the final sync is "
                                           "crash-consistent and later writes on the source are not migrated")
            vm = self.source.find_vm(self.job.vm)
            out = self.source.shutdown_vm(vm, force=force, timeout=timeout)
            record(source=out)
            return TaskOutcome(result={"source": out}, message=out)

        def finalize(ctx: TaskContext) -> TaskOutcome:
            d = next((x for x in self.state.deployments if x["id"] == self._deploy_id), {})
            record(status="done", target_vm=d.get("target_vm"), finished=time.time())
            return TaskOutcome(message=f"cutover complete: {name} is running on the target"
                                       + ("; the source is powered off (not deleted)" if shutdown else ""))

        return {**self.sync_handlers(), **dep, "check_cutover": check, "plan_cutover": plan,
                "shutdown_source": stop_source, "finalize_cutover": finalize}

    # ------------------------------------------------------------------ delete replica disks
    def delete_handlers(self) -> dict[str, Handler]:
        def plan(ctx: TaskContext) -> TaskOutcome:
            steps = [{"action": "target.delete_disk", "name": d["target"]["name"]}
                     for d in self.state.disks.values() if d.get("target")]
            steps += [{"action": "target.delete_restore_point", "id": r["id"]} for r in self.state.restore_points]
            if self.state.base_snapshot:
                steps.append({"action": "source.cleanup_snapshot", "id": self.state.base_snapshot["id"]})
            return TaskOutcome(result={"plan": steps}, message=f"{len(steps)} deletions planned (nothing changed)")

        def delete(ctx: TaskContext) -> TaskOutcome:
            n = 0
            for rp in list(self.state.restore_points):
                for did, sid in rp["disks"].items():
                    self.target.delete_disk_snapshot(_tdisk(self.state.disks[did]["target"]), sid)
                with self._lock:
                    self.state.restore_points.remove(rp)
                self._save()
            for did, d in self.state.disks.items():
                if d.get("target"):
                    self.target.delete_disk(_tdisk(d["target"]))
                    n += 1
                    with self._lock:
                        d["target"], d["synced"] = None, False
                    self._save()
            try:
                if self.state.base_snapshot:
                    self.source.cleanup(_snap(self.state.base_snapshot))
                if self.state.base_checkpoint:
                    self.source.release_checkpoint(CheckpointRef(**self.state.base_checkpoint))
            except Exception as e:  # noqa: BLE001 - source may be gone after a real disaster
                self.log.warning("could not release source snapshot/checkpoint: %s", e)
            with self._lock:
                self.state.base_snapshot = self.state.base_checkpoint = self.state.current = None
                self.state.consistent = False
            self._save()
            return TaskOutcome(message=f"deleted {n} replica disks (deployed VMs are kept)")

        return {"validate_target": self._validate_target, "plan_delete": plan, "delete_replica_disks": delete}

    # ------------------------------------------------------------------ failures
    def record_failure(self, kind: str, error: str) -> None:
        with self._lock:
            self.state.errors.append({"t": time.time(), "kind": kind, "error": error})
            self.state.errors = self.state.errors[-50:]
            if kind in ("replica_sync", "replica_cutover") and self.state.current:
                self.state.current["last_error"] = error
            if kind == "replica_cutover":
                for c in self.state.cutovers:
                    if c.get("status") == "running":
                        c["status"], c["error"] = "failed", error
        self._save()
        if kind in ("replica_deploy", "replica_cutover") and hasattr(self, "_deploy_runner") and any(
                d["id"] == self._deploy_id for d in self.state.deployments):
            dep = next((d for d in self.state.deployments if d["id"] == self._deploy_id), None)
            if dep is not None:
                dep["status"] = "failed"
                r = self._deploy_runner()
                with r._lock:
                    r.state.errors.append({"phase": r.state.phase.value, "error": error})
                    r.state.set_phase(Phase.FAILED)
                    r.store.save(r.state)
                if not self.ctx.dry_run and self.engine.on_failure == "cleanup":
                    self.target.cleanup_on_failure(r.state.created)
                self._save()
