"""Purpose: the provider-agnostic migration pipeline (one VM). Knows nothing about any platform.

Usage:
    runner = MigrationRunner(job, engine_cfg, source, target, ctx, StateStore(dir))
    state = runner.run()        # dry-run: inspect + gates + plan only; --execute: full migration

Phases (persisted; a re-run resumes from the last good phase):
  inspect (+ disk-count gate, preflight) -> snapshot (+ snapshot disk-count gate)
  -> create target disks -> per disk, in parallel: copy (resumable watermark) -> verify staged
     -> commit -> verify committed -> free staging
  -> OSMorph on committed images -> prepare boot disk -> create VM (data disks may attach here)
  -> attach remaining disks in order (+ attach-count gate) -> set boot -> boot check
  -> cleanup source snapshot -> done
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from ..config.loader import EngineConfig, MigrationConfig
from ..core.context import RunContext
from ..core.errors import ManualMorphRequired, SafetyGateError, TransientProviderError, VerificationError
from ..core.fileio import RawFileReader
from ..core.interfaces import DiskHints, SnapshotRef, SourceProvider, TargetDiskRef, TargetProvider, VmRef
from ..ir import DiskSpec, GuestOS, OsFamily, VmSpec
from ..osmorph import MorphRequest, resolve
from . import safety
from .copier import copy_disk
from .report import write_report
from .state import DiskState, MigrationState, Phase, StateStore, phase_at_least
from .verify import check_size, compare_samples, full_verify, sample_offsets


def _ref_to_dict(r: Any) -> dict[str, Any]:
    d = asdict(r)
    if "vm" in d and isinstance(d["vm"], dict):
        d["vm"] = dict(d["vm"])
    return {k: (dict(v) if k == "data" else v) for k, v in d.items()}


def _tdisk(d: dict[str, Any]) -> TargetDiskRef:
    return TargetDiskRef(id=d["id"], name=d["name"], size_bytes=d["size_bytes"], data=d.get("data", {}))


def _snap(d: dict[str, Any]) -> SnapshotRef:
    return SnapshotRef(id=d["id"], vm=VmRef(**d["vm"]), app_consistent=d["app_consistent"], data=d.get("data", {}))


class MigrationRunner:
    def __init__(
        self,
        job: MigrationConfig,
        engine: EngineConfig,
        source: SourceProvider,
        target: TargetProvider,
        ctx: RunContext,
        store: StateStore,
        *,
        shared: Any = None,
        migration_id: str | None = None,
    ):
        from .shared import MemorySharedDisks
        self.shared = shared if shared is not None else MemorySharedDisks()
        self.migration_id = migration_id or job.name
        #: how long a cluster member waits for the owner to create / finish a shared disk
        self.shared_create_wait = 4 * 3600
        self.shared_copy_wait = 7 * 86400
        self.shared_poll = 15.0
        self.job = job
        self.engine = engine
        self.source = source
        self.target = target
        self.ctx = ctx
        self.store = store
        self.log = ctx.logger.getChild(job.name) if isinstance(ctx.logger, logging.Logger) else ctx.logger
        self._lock = threading.RLock()
        self.state = store.load_or_new(job.name, source=job.source, target=job.target)
        #: optional (disk_id, bytes_done, size) callback — the task engine uses it for live progress
        self.progress_hook: Any = None
        #: optional () -> None that raises to abort a copy (task cancel)
        self.cancel_hook: Any = None
        #: optional (disk_id, message) callback: what a disk is doing after/around its copy
        self.step_hook: Any = None

    # ------------------------------------------------------------------ helpers
    def _save(self) -> None:
        with self._lock:
            self.store.save(self.state)

    def _phase(self, p: Phase) -> None:
        with self._lock:
            self.state.set_phase(p)
            self.store.save(self.state)
        self.log.info("phase -> %s", p.value)

    def _done(self, p: Phase) -> bool:
        return phase_at_least(self.state.resume_phase(), p)

    @property
    def target_name(self) -> str:
        return self.job.target_name or (self.state.spec or {}).get("name") or self.job.vm

    def _disk_name(self, d: DiskSpec) -> str:
        return f"{self.target_name}-disk{d.order:02d}"

    # ------------------------------------------------------------------ phases
    def _inspect(self) -> tuple[VmRef, VmSpec]:
        vm = self.source.find_vm(self.job.vm)
        spec = self.source.inspect_vm(vm)
        if self.job.guest_os:
            g = self.job.guest_os
            spec.guest_os = GuestOS(family=OsFamily(g.get("family", spec.guest_os.family)),
                                    distro=g.get("distro", spec.guest_os.distro),
                                    version=g.get("version", spec.guest_os.version),
                                    bits=int(g.get("bits", spec.guest_os.bits)))
        spec.extra["target_name"] = self.job.target_name or spec.name
        spec.extra["target_options"] = dict(self.job.target_options)
        spec.extra["max_parallel_disks"] = self.engine.max_parallel_disks  # for target headroom checks
        spec.validate()
        self._check_shared(spec)

        counts = safety.check_disk_counts(spec, self.source.disk_count_crosscheck(vm))
        if len(spec.disks) >= safety.LEGACY_DISK_CAP:
            self.log.warning(
                "VM %s has %d disks (>= legacy cap %d) — all enumerated and cross-checked: %s",
                spec.name, len(spec.disks), safety.LEGACY_DISK_CAP, [d.address for d in spec.ordered_disks()],
            )
        if self.state.spec is not None:
            before = sorted(d["id"] for d in self.state.spec["disks"])
            now = sorted(d.id for d in spec.disks)
            if before != now:
                raise SafetyGateError(f"{spec.name}: disk set changed since this migration started ({before} -> {now})")
        problems = self.source.preflight(spec) + self.target.preflight(spec)
        if problems:
            raise SafetyGateError("preflight failed:\n  - " + "\n  - ".join(problems))
        with self._lock:
            self.state.spec = spec.to_dict()
            self.state.crosscheck = counts
            for d in spec.disks:
                self.state.disks.setdefault(d.id, DiskState(disk_id=d.id, order=d.order, size_bytes=d.size_bytes))
        if not self._done(Phase.INSPECTED):
            self._phase(Phase.INSPECTED)
        else:
            self._save()
        return vm, spec

    def _plan(self, spec: VmSpec) -> None:
        c = self.ctx
        app = self.job.app_consistent if self.job.app_consistent is not None else self.engine.app_consistent
        c.plan("source.create_snapshot", vm=spec.name, app_consistent=app)
        for d in spec.ordered_disks():
            c.plan("target.create_disk", name=self._disk_name(d), size_bytes=d.size_bytes, bus=d.bus.value,
                   source=d.address, boot=d.is_boot)
        c.plan("engine.copy_verify_commit_disks", disks=len(spec.disks), total_bytes=spec.total_disk_bytes(),
               parallel=self.engine.max_parallel_disks, chunk=self.engine.chunk_size)
        # Placeholder images: the plan then reports "auto" iff the tooling (guestfish) exists on
        # this host, which is what the --execute run will see once disks are committed.
        placeholders = [(Path(f"<{self._disk_name(d)}>"), "qcow2") for d in spec.ordered_disks()]
        morph = resolve(spec.guest_os.family, self.target.platform_family).run(
            MorphRequest(spec, self.target.platform_family, self.target.target_bus(spec), placeholders,
                         self._morph_options()),
            self.ctx,
        )
        c.plan("osmorph", status=morph.status, strategy=morph.strategy, steps=morph.steps)
        c.plan("target.prepare_boot_disk", bus=self.target.target_bus(spec).value, firmware=spec.firmware.value)
        c.plan("target.create_vm", name=self.target_name, cpu=spec.cpu.total, memory_bytes=spec.memory_bytes,
               nics=len(spec.nics))
        c.plan("target.attach_disks", count=len(spec.disks) - 1)
        if self.engine.boot_check:
            c.plan("target.check_boot", timeout=self.engine.boot_timeout)
        if self.engine.cleanup_source_snapshot:
            c.plan("source.cleanup_snapshot")
        with self._lock:
            self.state.morph = morph.to_dict()

    def _snapshot(self, vm: VmRef, spec: VmSpec) -> SnapshotRef:
        if self.state.snapshot:
            snap = _snap(self.state.snapshot)
            self.log.info("reusing snapshot %s", snap.id)
        else:
            app = self.job.app_consistent if self.job.app_consistent is not None else self.engine.app_consistent
            snap = self.source.create_snapshot(vm, app_consistent=app)
            with self._lock:
                self.state.snapshot = _ref_to_dict(snap)
                self.state.created["source"].append(_ref_to_dict(snap))
                # a fresh snapshot invalidates any partial copy from a previous snapshot
                for ds in self.state.disks.values():
                    ds.bytes_copied, ds.copied, ds.committed = 0, False, False
            self._phase(Phase.SNAPSHOTTED)
        safety.check_snapshot_disks(spec, self.source.snapshot_disk_count(snap))
        return safety.require_snapshot(snap)

    # ------------------------------------------------------------------ cluster shared disks
    def _check_shared(self, spec: VmSpec) -> None:
        shared = [d for d in spec.disks if d.shared_id]
        if not shared:
            return
        ids = ", ".join(f"{d.address} ({d.shared_id})" for d in shared)
        if not self.job.cluster:
            raise SafetyGateError(f"{spec.name}: disk(s) {ids} are shared with other VMs (cluster). Migrate every "
                                  "member with the same options.cluster so each shared disk is copied once and "
                                  "attached to all of them.")
        if any(d.is_boot for d in shared):
            raise SafetyGateError(f"{spec.name}: the boot disk is shared; not supported")
        ok, why = self.target.supports_shared_disks()
        if not ok:
            raise SafetyGateError(f"{spec.name}: shared disks {ids}: {why}")

    def _shared_wait(self, d: DiskSpec, want: set[str], timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            row = self.shared.get(self.job.cluster, d.shared_id)
            if row and row["status"] in want:
                return row
            if row and row["status"] == "failed":
                raise VerificationError(f"shared disk {d.shared_id}: the owner migration {row['owner']} failed it")
            if time.monotonic() > deadline:
                raise TransientProviderError(f"shared disk {d.shared_id}: still {row and row['status']} after "
                                             f"{int(timeout)}s (owner {row and row['owner']})")
            if self.cancel_hook is not None:
                self.cancel_hook()
            time.sleep(self.shared_poll)

    def _step(self, d: DiskSpec, msg: str) -> None:
        if self.step_hook is not None:
            self.step_hook(d.id, msg)

    def _is_member(self, d: DiskSpec) -> bool:
        """True when this migration attaches a shared disk another migration copies."""
        return bool(d.shared_id and self.state.disks[d.id].shared_owner
                    and self.state.disks[d.id].shared_owner != self.migration_id)

    def _create_disks(self, spec: VmSpec) -> dict[str, TargetDiskRef]:
        refs: dict[str, TargetDiskRef] = {}
        for d in spec.ordered_disks():
            ds = self.state.disks[d.id]
            if ds.target:
                refs[d.id] = _tdisk(ds.target)
                continue
            if d.shared_id:
                row = self.shared.claim(self.job.cluster, d.shared_id, self.migration_id, d.size_bytes)
                with self._lock:
                    ds.shared_owner = row["owner"]
                if row["owner"] != self.migration_id:
                    self.log.info("disk %s: shared disk %s is copied by migration %s; waiting for its target disk",
                                  d.address, d.shared_id, row["owner"])
                    row = self._shared_wait(d, {"created", "done"}, self.shared_create_wait)
                    ref = _tdisk(row["target"])
                    with self._lock:
                        ds.target = _ref_to_dict(ref)        # NOT in created[]: a member never deletes it
                    self._save()
                    refs[d.id] = ref
                    continue
            name = self._disk_name(d) if not d.shared_id else f"{self.job.cluster}-shared-{d.shared_id}"[:200]
            ref = self.target.find_disk(name) or self.target.create_disk(
                name, d.size_bytes, DiskHints(bus=d.bus, is_boot=d.is_boot, order=d.order,
                                              storage_pool=self.job.target_options.get("storage_pool"),
                                              shared=bool(d.shared_id))
            )
            ok, msg = check_size(d.size_bytes, ref.size_bytes)
            if not ok:
                raise VerificationError(f"{name}: {msg}")
            with self._lock:
                ds.target = _ref_to_dict(ref)
                self.state.created["target_disks"].append(_ref_to_dict(ref))
            self._save()
            if d.shared_id:
                self.shared.update(self.job.cluster, d.shared_id, self.migration_id, status="created",
                                   target=_ref_to_dict(ref))
            refs[d.id] = ref
        if not self._done(Phase.DISKS_CREATED):
            self._phase(Phase.DISKS_CREATED)
        return refs

    def _verify_against_source(self, snap: SnapshotRef, d: DiskSpec, dst) -> dict[str, Any]:
        offs = sample_offsets(d.id, d.size_bytes, count=self.engine.verify_samples,
                              length=self.engine.verify_sample_bytes)
        with self.source.open_disk(snap, d) as src, dst:
            res = compare_samples(src, dst, offs, length=self.engine.verify_sample_bytes)
        if not res.ok:
            raise VerificationError(f"disk {d.id} ({d.address}): {res.detail} at {res.mismatches[:5]}")
        return res.to_dict()

    def _copy_one(self, snap: SnapshotRef, d: DiskSpec, ref: TargetDiskRef) -> None:
        """copy -> verify staged -> commit -> verify committed -> free staging (one disk)."""
        ds = self.state.disks[d.id]
        if self._is_member(d):
            if not ds.copied:
                self._step(d, f"waiting for shared disk {d.shared_id} (copied by migration {ds.shared_owner})")
                row = self._shared_wait(d, {"done"}, self.shared_copy_wait)
                with self._lock:
                    ds.copied = ds.committed = True
                    ds.bytes_copied = d.size_bytes
                    ds.verify = {"ok": True, "shared": f"copied and verified by migration {row['owner']}"}
                    self.store.save(self.state)
            return
        if not ds.copied:
            start = ds.bytes_copied
            last_pct = [-1]
            t_start = time.monotonic()
            last_save = [0.0]

            def progress(off: int, size: int) -> None:
                if self.cancel_hook is not None:
                    self.cancel_hook()
                if self.progress_hook is not None:
                    self.progress_hook(d.id, off, size)
                pct = off * 100 // max(size, 1)
                if pct // 5 != last_pct[0] // 5:
                    last_pct[0] = pct
                    self.log.info("disk %02d %s: %d%%", d.order, d.address, pct)
                now = time.monotonic()
                if now - last_save[0] >= 2.0:  # throttled live progress for the UI
                    last_save[0] = now
                    with self._lock:
                        ds.progress_bytes = off
                        ds.rate_mb_s = ((off - start) / (1 << 20)) / max(now - t_start, 1e-6)
                        ds.progress_at = time.time()
                        self.store.save(self.state)

            def checkpoint(off: int) -> None:
                with self._lock:
                    ds.bytes_copied = off
                    self.store.save(self.state)

            with self.source.open_disk(snap, d) as reader, \
                    self.target.open_disk_writer(ref, resume=start > 0) as writer:
                if reader.size != d.size_bytes:
                    raise SafetyGateError(f"disk {d.id}: snapshot size {reader.size} != inspected size {d.size_bytes}")
                # sources that know their allocation (e.g. VMware CBT "*") let a fresh, zeroed target skip
                # never-written regions entirely
                alloc = reader.allocated_ranges() if writer.prezeroed else None
                stats = copy_disk(reader, writer, chunk_size=self.engine.chunk_size, start_offset=start, ranges=alloc,
                                  checkpoint_every=self.engine.checkpoint_every, on_progress=progress,
                                  on_checkpoint=checkpoint)
                staged = writer.staged_path
            verify: dict[str, Any] = {"size": check_size(d.size_bytes, ref.size_bytes)[1]}
            if staged is not None:
                self._step(d, "copied; verifying samples against the source")
                verify["staged_samples"] = self._verify_against_source(snap, d, RawFileReader(staged))
            with self._lock:
                ds.copied = True
                ds.staged_path = str(staged) if staged else None
                ds.copy_stats = {**asdict(stats), "mb_per_s": stats.mb_per_s}
                ds.verify = {"ok": True, **verify}
                self.store.save(self.state)
            self.log.info("disk %02d copied: %.1f MB/s, %d B zero-skipped",
                          d.order, stats.mb_per_s, stats.bytes_zero_skipped)
        if not ds.committed:
            self._step(d, "committing to the target disk")
            self.target.commit_disk(ref, Path(ds.staged_path) if ds.staged_path else None)
            reader = self.target.open_disk_reader(ref)
            if reader is not None:
                self._step(d, "verifying the committed disk")
                res = self._verify_against_source(snap, d, reader)
                with self._lock:
                    ds.verify = {**(ds.verify or {}), "target_samples": res}
            with self._lock:
                ds.committed = True
                self.store.save(self.state)
            if self.engine.verify_full:
                self.log.info("disk %02d: full SHA-256 verification (reads source and target completely)", d.order)
                last = [-1]

                def fv_progress(done: int, total: int) -> None:
                    pct = done * 100 // max(total, 1)
                    if pct != last[0]:
                        last[0] = pct
                        self._step(d, f"full SHA-256 checksum {pct}%")
                with self.source.open_disk(snap, d) as src:
                    res = full_verify(src, self.target.open_disk_reader(ref), d.size_bytes, chunk=self.engine.chunk_size,
                                      progress=fv_progress)
                with self._lock:
                    ds.verify = {**(ds.verify or {}), "full": res}
                    self.store.save(self.state)
                if res.get("skipped"):
                    self.log.warning("disk %02d: %s", d.order, res["skipped"])
                elif not res["ok"]:
                    raise VerificationError(f"disk {d.id}: full checksum mismatch "
                                            f"(source {res['source_sha256'][:16]}..., target {res['target_sha256'][:16]}...)")
            if d.shared_id:
                self.shared.update(self.job.cluster, d.shared_id, self.migration_id, status="done")
            self.target.discard_staging(ref)
            self.log.info("disk %02d committed", d.order)

    def _parallel(self, fn, items) -> None:
        errors: list[BaseException] = []
        with ThreadPoolExecutor(max_workers=self.engine.max_parallel_disks, thread_name_prefix="disk") as ex:
            futs = {ex.submit(fn, *it): it for it in items}
            for f in as_completed(futs):
                e = f.exception()
                if e is not None:
                    errors.append(e)
        if errors:
            raise errors[0]

    def _copy(self, snap: SnapshotRef, spec: VmSpec, refs: dict[str, TargetDiskRef]) -> None:
        # Pitfall 10: single-stream reads cap out; parallelize ACROSS disks (largest first).
        # Each disk is committed as soon as it is copied, so staging only ever holds
        # max_parallel_disks images at once.
        # cluster members wait for shared disks LAST, after every disk they copy themselves
        todo = sorted(spec.disks, key=lambda d: (self._is_member(d), -d.size_bytes))
        self._parallel(lambda d: self._copy_one(snap, d, refs[d.id]), [(d,) for d in todo])
        self._phase(Phase.DISKS_COPIED)

    def _morph_options(self) -> dict:
        """OSMorph options plus the target facts a strategy needs (MACs kept on the target?)."""
        o = dict(self.job.osmorph)
        o.setdefault("preserve_mac", bool(self.job.target_options.get("preserve_mac", False)))
        return o

    def _morph(self, spec: VmSpec, refs: dict[str, TargetDiskRef]) -> None:
        # shared cluster disks are never edited by OSMorph (another member owns / uses them)
        images = [self.target.morph_image(refs[d.id]) for d in spec.ordered_disks() if not d.shared_id]
        usable = [i for i in images if i is not None] if all(images) else []
        bus = self.target.target_bus(spec)
        strategy = resolve(spec.guest_os.family, self.target.platform_family)
        res = strategy.run(MorphRequest(spec, self.target.platform_family, bus, usable, self._morph_options()), self.ctx)
        with self._lock:
            self.state.morph = res.to_dict()
        self._save()
        if res.status == "manual_required":
            msg = "; ".join(res.notes) or res.steps
            if not self.job.osmorph.get("allow_manual", False):
                raise ManualMorphRequired(
                    f"{spec.name}: {msg}. Fix the guest manually, then set osmorph.allow_manual: true to continue.")
            self.log.error("MANUAL OSMORPH REQUIRED (continuing because allow_manual=true): %s", msg)
        for n in res.notes:
            self.log.warning("osmorph: %s", n)
        self._phase(Phase.MORPHED)

    def _create_vm(self, spec: VmSpec, refs: dict[str, TargetDiskRef]) -> VmRef:
        boot = spec.boot_disk()
        if self.state.target_vm:
            return VmRef(**self.state.target_vm)
        existing = self.target.find_vm(self.target_name)
        if existing is not None:
            # Adopt only a VM this migration started creating (crash between create and save).
            if self.state.created.get("target_vm_pending") != self.target_name:
                raise SafetyGateError(f"target already has a VM named {self.target_name!r} not created by this migration")
            vm = existing
        else:
            self.target.prepare_boot_disk(refs[boot.id], spec, self.target.target_bus(spec))
            with self._lock:
                self.state.created["target_vm_pending"] = self.target_name
            self._save()
            data = [refs[d.id] for d in spec.ordered_disks() if d.id != boot.id]
            vm = self.target.create_vm(replace(spec, name=self.target_name), refs[boot.id], data)
        with self._lock:
            self.state.target_vm = _ref_to_dict(vm)
            self.state.created["target_vm"] = _ref_to_dict(vm)
            self.state.disks[boot.id].attached = True
        self._phase(Phase.VM_CREATED)
        return vm

    def _attach(self, spec: VmSpec, vm: VmRef, refs: dict[str, TargetDiskRef]) -> None:
        attached = set(self.target.list_attached_disks(vm))
        for d in spec.ordered_disks():
            ds = self.state.disks[d.id]
            if refs[d.id].id in attached:
                ds.attached = True
                continue
            self.target.attach_disk(vm, refs[d.id], d.order)
            with self._lock:
                ds.attached = True
                self.store.save(self.state)
        boot = spec.boot_disk()
        self.target.set_boot(vm, refs[boot.id], spec.firmware, self.target.target_bus(spec))
        final = self.target.list_attached_disks(vm)
        safety.check_target_attach(spec, final)
        expected = [refs[d.id].id for d in spec.ordered_disks()]
        if final != expected:
            self.log.warning("attach order differs from source order: expected %s got %s", expected, final)
        self._phase(Phase.DISKS_ATTACHED)

    def _verify(self, spec: VmSpec, vm: VmRef) -> None:
        summary: dict[str, Any] = {
            "disk_count": {"source": len(spec.disks), "target_attached": len(self.target.list_attached_disks(vm))},
            "disks_verified": sum(1 for d in self.state.disks.values() if (d.verify or {}).get("ok")),
        }
        if self.engine.boot_check:
            bc = self.target.check_boot(vm, timeout=self.engine.boot_timeout)
            summary["boot"] = {"ok": bc.ok, "detail": bc.detail, "evidence": bc.evidence[-2000:]}
            if not bc.ok:
                with self._lock:
                    self.state.verification = summary
                raise VerificationError(f"boot check failed: {bc.detail}")
        with self._lock:
            self.state.verification = summary
        self._phase(Phase.VERIFIED)

    # ------------------------------------------------------------------ driver
    def run(self) -> MigrationState:
        if self.state.phase == Phase.DONE:
            self.log.info("migration %s already done; nothing to do", self.job.name)
            return self.state
        try:
            vm, spec = self._inspect()
            if self.ctx.dry_run:
                self._plan(spec)
                self._save()
                write_report(self.state, self.engine.report_dir, planned=self.ctx.planned)
                return self.state
            snap = self._snapshot(vm, spec)
            refs = self._create_disks(spec)
            if not self._done(Phase.DISKS_COPIED):
                self._copy(snap, spec, refs)
            if not self._done(Phase.MORPHED):
                self._morph(spec, refs)
            tvm = self._create_vm(spec, refs)
            if not self._done(Phase.DISKS_ATTACHED):
                self._attach(spec, tvm, refs)
            if not self._done(Phase.VERIFIED):
                self._verify(spec, tvm)
            if self.engine.cleanup_source_snapshot and self.state.snapshot:
                self.source.cleanup(snap)
                with self._lock:
                    self.state.snapshot = None
            self._phase(Phase.DONE)
            return self.state
        except BaseException as e:
            with self._lock:
                self.state.errors.append({"phase": self.state.phase.value, "error": f"{type(e).__name__}: {e}"})
                self.state.set_phase(Phase.FAILED)
                self.store.save(self.state)
            self.log.error("migration %s FAILED in phase %s: %s", self.job.name, self.state.last_good_phase.value, e)
            if not self.ctx.dry_run and self.engine.on_failure == "cleanup":
                self.target.cleanup_on_failure(self.state.created)
            raise
        finally:
            if not self.ctx.dry_run:
                write_report(self.state, self.engine.report_dir)
