"""Purpose: map the provider-agnostic migration pipeline onto the task DAG. This is the "what tasks
does a Migration execution consist of" definition (Coriolis parity: ValidateEndpoints ->
DeployReplicaDisks -> ReplicateDisks -> OSMorphing -> DeployInstance -> Finalize), with the same
safety gates as the single-process runner because every handler delegates to MigrationRunner.

Usage:
    specs = migration_dag(dry_run=False)                 # initial graph (conductor builds this)
    handlers = migration_handlers(runner)                # runner = MigrationRunner(...) (worker)
    status = DagRunner(store, handlers, max_parallel=engine.max_parallel_disks).run()
    if status != ExecStatus.COMPLETED: record_failure(runner, "...")

Graph (execute):
  validate_endpoints -> inspect_instance (DISK-COUNT GATE, preflight) -> create_snapshot
  -> deploy_disks ==expands==> replicate_disk:<disk-id> x N (parallel, largest first)
  -> osmorphing -> deploy_instance (create VM + attach + attach-count gate) -> verify_instance
  -> finalize (cleanup source snapshot, report)
Graph (dry-run): validate_endpoints -> inspect_instance -> plan   (nothing mutating is reachable)
"""

from __future__ import annotations

import threading
from dataclasses import asdict
from typing import Any

from ..core.errors import SafetyGateError
from ..core.interfaces import TargetDiskRef, VmRef
from ..ir import VmSpec
from .dag import COPY_ATTEMPTS, Handler, TaskContext, TaskOutcome, TaskSpec
from .pipeline import MigrationRunner, _snap, _tdisk, keep_target_vm
from .report import write_report
from .state import Phase

MIGRATION_TASKS = ["validate_endpoints", "inspect_instance", "create_snapshot", "deploy_disks",
                   "replicate_disk", "osmorphing", "deploy_instance", "verify_instance", "finalize", "plan"]


def migration_dag(dry_run: bool) -> list[TaskSpec]:
    t = [
        TaskSpec("validate_endpoints", "validate_endpoints", label="Validate endpoints"),
        TaskSpec("inspect_instance", "inspect_instance", ["validate_endpoints"],
                 label="Inspect VM + disk-count gate"),
    ]
    if dry_run:
        return t + [TaskSpec("plan", "plan", ["inspect_instance"], label="Build dry-run plan")]
    return t + [
        TaskSpec("create_snapshot", "create_snapshot", ["inspect_instance"], label="Snapshot source"),
        TaskSpec("deploy_disks", "deploy_disks", ["create_snapshot"], label="Create target disks"),
        TaskSpec("osmorphing", "osmorphing", ["deploy_disks"], label="OSMorphing"),
        TaskSpec("deploy_instance", "deploy_instance", ["osmorphing"], label="Create VM + attach disks"),
        TaskSpec("verify_instance", "verify_instance", ["deploy_instance"], label="Verify (counts, boot)"),
        TaskSpec("finalize", "finalize", ["verify_instance"], label="Finalize + cleanup snapshot"),
    ]


def _gib(n: int) -> str:
    """Human size with a sensible unit (labels and messages): 512.0 MiB, 1.5 GiB, 2.0 TiB."""
    for unit, shift in (("TiB", 40), ("GiB", 30), ("MiB", 20)):
        if n >= 1 << shift:
            return f"{n / (1 << shift):.1f} {unit}"
    return f"{n} B"


def migration_handlers(runner: MigrationRunner) -> dict[str, Handler]:
    """Handlers bound to one MigrationRunner (one VM). Per-disk progress is routed to the task that
    owns the disk, so the UI gets one progress bar per disk."""
    disk_ctx: dict[str, TaskContext] = {}
    lock = threading.Lock()

    def on_disk_progress(disk_id: str, off: int, size: int) -> None:
        with lock:
            ctx = disk_ctx.get(disk_id)
        if ctx is not None:
            ds = runner.state.disks.get(disk_id)
            rate = f", {ds.rate_mb_s:.0f} MB/s" if ds and ds.rate_mb_s else ""
            ctx.progress(off * 100 / max(size, 1), f"{off / (1 << 30):.1f}/{size / (1 << 30):.1f} GiB{rate}")

    def on_disk_step(disk_id: str, msg: str) -> None:
        with lock:
            ctx = disk_ctx.get(disk_id)
        if ctx is not None:
            ctx.progress(ctx.rec.progress, msg, force=True)

    runner.progress_hook = on_disk_progress
    runner.step_hook = on_disk_step

    def spec() -> VmSpec:
        if runner.state.spec is None:
            raise SafetyGateError("no inspected VM spec in state (inspect_instance did not run)")
        return VmSpec.from_dict(runner.state.spec)

    def refs() -> dict[str, TargetDiskRef]:
        out = {}
        for d in spec().disks:
            t = runner.state.disks[d.id].target
            if not t:
                raise SafetyGateError(f"disk {d.id} has no target disk recorded")
            out[d.id] = _tdisk(t)
        return out

    def validate_endpoints(ctx: TaskContext) -> TaskOutcome:
        vm = runner.source.find_vm(runner.job.vm)
        problems = runner.source.preflight(None) + runner.target.preflight(None)
        if problems:
            raise SafetyGateError("endpoint validation failed:\n  - " + "\n  - ".join(problems))
        return TaskOutcome(result={"source_vm": asdict(vm)}, message=f"found {vm.name}")

    def inspect_instance(ctx: TaskContext) -> TaskOutcome:
        vm, s = runner._inspect()
        return TaskOutcome(
            result={"vm": asdict(vm), "disks": len(s.disks), "total_bytes": s.total_disk_bytes(),
                    "crosscheck": runner.state.crosscheck},
            message=f"{len(s.disks)} disks, {_gib(s.total_disk_bytes())}; counts agree: {runner.state.crosscheck}")

    def plan(ctx: TaskContext) -> TaskOutcome:
        runner._plan(spec())
        runner._save()
        write_report(runner.state, runner.engine.report_dir, planned=runner.ctx.planned)
        steps = [{"action": a.action, **a.details} for a in runner.ctx.planned]
        return TaskOutcome(result={"plan": steps}, message=f"{len(steps)} planned actions (nothing changed)")

    def create_snapshot(ctx: TaskContext) -> TaskOutcome:
        vm = VmRef(**ctx.result("inspect_instance")["vm"]) if ctx.result("inspect_instance") \
            else runner.source.find_vm(runner.job.vm)
        snap = runner._snapshot(vm, spec())
        return TaskOutcome(result={"snapshot_id": snap.id, "app_consistent": snap.app_consistent},
                           message=f"snapshot {snap.id}")

    def deploy_disks(ctx: TaskContext) -> TaskOutcome:
        s = spec()
        r = runner._create_disks(s)
        children = [
            TaskSpec(f"replicate_disk:{d.id}", "replicate_disk", params={"disk_id": d.id, "size_bytes": d.size_bytes},
                     max_attempts=COPY_ATTEMPTS,
                     label=f"Disk {d.order:02d} {d.address} ({_gib(d.size_bytes)})")
            for d in sorted(s.disks, key=lambda d: -d.size_bytes)   # pitfall 10: largest first
        ]
        return TaskOutcome(result={"target_disks": {k: v.id for k, v in r.items()}}, children=children,
                           message=f"{len(r)} target disks ready")

    def replicate_disk(ctx: TaskContext) -> TaskOutcome:
        did = ctx.params["disk_id"]
        s = spec()
        d = next(x for x in s.disks if x.id == did)
        with lock:
            disk_ctx[did] = ctx
        try:
            runner._copy_one(_snap(runner.state.snapshot), d, refs()[did])  # type: ignore[arg-type]
        finally:
            with lock:
                disk_ctx.pop(did, None)
        ds = runner.state.disks[did]
        return TaskOutcome(result={"copy_stats": ds.copy_stats, "verify": ds.verify},
                           message=f"copied + verified ({(ds.copy_stats or {}).get('mb_per_s', 0):.0f} MB/s)")

    def osmorphing(ctx: TaskContext) -> TaskOutcome:
        if not runner._done(Phase.DISKS_COPIED):
            runner._phase(Phase.DISKS_COPIED)
        runner._morph(spec(), refs())
        m = runner.state.morph or {}
        return TaskOutcome(result=m, message=f"{m.get('strategy')}: {m.get('status')}")

    def deploy_instance(ctx: TaskContext) -> TaskOutcome:
        s, r = spec(), refs()
        vm = runner._create_vm(s, r)
        if not runner._done(Phase.DISKS_ATTACHED):
            runner._attach(s, vm, r)
        return TaskOutcome(result={"target_vm": asdict(vm)}, message=f"VM {vm.name} with {len(r)} disks")

    def verify_instance(ctx: TaskContext) -> TaskOutcome:
        if not runner._done(Phase.VERIFIED):
            runner._verify(spec(), VmRef(**runner.state.target_vm))  # type: ignore[arg-type]
        return TaskOutcome(result=runner.state.verification or {}, message="verified")

    def finalize(ctx: TaskContext) -> TaskOutcome:
        if runner.engine.cleanup_source_snapshot and runner.state.snapshot:
            runner.source.cleanup(_snap(runner.state.snapshot))
            with runner._lock:
                runner.state.snapshot = None
        runner._phase(Phase.DONE)
        write_report(runner.state, runner.engine.report_dir)
        return TaskOutcome(message="done")

    return {
        "validate_endpoints": validate_endpoints, "inspect_instance": inspect_instance, "plan": plan,
        "create_snapshot": create_snapshot, "deploy_disks": deploy_disks, "replicate_disk": replicate_disk,
        "osmorphing": osmorphing, "deploy_instance": deploy_instance, "verify_instance": verify_instance,
        "finalize": finalize,
    }


def record_failure(runner: MigrationRunner, error: str, *, cleanup: bool | None = None) -> bool:
    """Mirror MigrationRunner.run()'s failure path for a DAG execution: persist the error and FAILED
    phase, optionally clean up what this migration created on the target, write the report.
    Returns True when the cleanup ran: the target disks are gone, so the caller must make a retry
    start again from disk creation (see dag.reset_after_cleanup)."""
    with runner._lock:
        runner.state.errors.append({"phase": runner.state.phase.value, "error": error})
        runner.state.set_phase(Phase.FAILED)
        runner.store.save(runner.state)
    do_cleanup = (runner.engine.on_failure == "cleanup") if cleanup is None else cleanup
    if do_cleanup and keep_target_vm(runner.state, runner.log):
        do_cleanup = False
    cleaned = False
    if not runner.ctx.dry_run and do_cleanup:
        runner.target.cleanup_on_failure(runner.state.created)
        with runner._lock:
            runner.state.forget_target()
            runner.store.save(runner.state)
        cleaned = True
    write_report(runner.state, runner.engine.report_dir)
    return cleaned


def summarize(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Compact per-execution summary for the API (counts from inspect + verify)."""
    insp = results.get("inspect_instance", {})
    return {"disks": insp.get("disks"), "crosscheck": insp.get("crosscheck"),
            "verification": results.get("verify_instance")}
