"""Purpose: `vmigrate` command line. Dry-run is the default for everything; --execute is required to
change anything on either platform.

Usage:
    vmigrate validate  -c migrate.yaml
    vmigrate matrix
    vmigrate list-vms  -c migrate.yaml --source ahv1
    vmigrate inspect   -c migrate.yaml --source ahv1 --vm RIS-Sainath   # IR + disk-count gate
    vmigrate migrate   -c migrate.yaml [--only ris-sainath] [--execute]
    vmigrate status    -c migrate.yaml
    vmigrate cleanup   -c migrate.yaml --migration ris-sainath [--execute]
Options: --log-json, --log-level DEBUG|INFO|WARNING
Exit codes: 0 ok, 1 migration/provider error, 2 config error, 3 safety gate refused.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from typing import Any

from ..config import load_config
from ..core.context import RunContext
from ..core.errors import ConfigError, MigrationError, SafetyGateError
from ..engine import MigrationRunner, StateStore
from ..engine.safety import check_disk_counts
from ..osmorph import registered
from ..providers import build_source, build_target, support_matrix


class JsonFormatter(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        d: dict[str, Any] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(r.created)), "level": r.levelname,
                             "logger": r.name, "thread": r.threadName, "msg": r.getMessage()}
        if r.exc_info:
            d["exc"] = self.formatException(r.exc_info)
        return json.dumps(d)


def setup_logging(level: str, as_json: bool) -> logging.Logger:
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(JsonFormatter() if as_json else
                   logging.Formatter("%(asctime)s %(levelname)-7s %(name)s [%(threadName)s] %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level)
    return logging.getLogger("vmigrate")


def _ctx(args: argparse.Namespace, log: logging.Logger) -> RunContext:
    return RunContext(dry_run=not getattr(args, "execute", False), logger=log)


def cmd_validate(args, log) -> int:
    cfg = load_config(args.config)
    for w in cfg.warnings:
        log.warning(w)
    print(f"OK: {len(cfg.sources)} source(s), {len(cfg.targets)} target(s), {len(cfg.migrations)} migration(s)")
    return 0


def cmd_matrix(args, log) -> int:
    print(f"{'side':7} {'platform':10} {'status':12} notes")
    for side, name, status, notes in support_matrix():
        print(f"{side:7} {name:10} {status:12} {notes}")
    print("\nOSMorph strategies (auto):", ", ".join(f"{f}->{p}" for f, p in registered()) or "none")
    print("All other (guest, target) cells: manual OSMorph required")
    return 0


def cmd_list_vms(args, log) -> int:
    cfg = load_config(args.config)
    src = build_source(args.source, cfg.sources[args.source], _ctx(args, log))
    for v in src.list_vms():
        print(f"{v.id}  {v.name}")
    return 0


def cmd_inspect(args, log) -> int:
    cfg = load_config(args.config)
    src = build_source(args.source, cfg.sources[args.source], _ctx(args, log))
    vm = src.find_vm(args.vm)
    spec = src.inspect_vm(vm)
    counts = src.disk_count_crosscheck(vm)
    print(json.dumps(spec.to_dict(), indent=2, default=str))
    check_disk_counts(spec, counts)  # raises -> exit 3
    print(f"\nDISK-COUNT GATE PASS: {counts} ; disks: {[d.address for d in spec.ordered_disks()]}", file=sys.stderr)
    return 0


def cmd_migrate(args, log) -> int:
    cfg = load_config(args.config)
    for w in cfg.warnings:
        log.warning(w)
    jobs = [m for m in cfg.migrations if not args.only or m.name in args.only]
    if args.only and len(jobs) != len(args.only):
        raise ConfigError(f"unknown migration(s): {sorted(set(args.only) - {m.name for m in jobs})}")
    ctx = _ctx(args, log)
    log.info("mode: %s", "EXECUTE" if not ctx.dry_run else "DRY-RUN (no changes; pass --execute to migrate)")
    store = StateStore(cfg.engine.state_dir)
    rc = 0
    for job in jobs:
        jctx = RunContext(dry_run=ctx.dry_run, run_id=ctx.run_id, logger=log)
        src = build_source(job.source, cfg.sources[job.source], jctx)
        tgt = build_target(job.target, cfg.targets[job.target], jctx)
        try:
            st = MigrationRunner(job, cfg.engine, src, tgt, jctx, store).run()
            log.info("%s: %s", job.name, st.phase.value)
            if jctx.dry_run:
                print(f"\n== {job.name}: dry-run plan ==")
                for i, a in enumerate(jctx.planned, 1):
                    print(f"{i:2}. {a.action} " + " ".join(f"{k}={v}" for k, v in a.details.items()))
        except SafetyGateError as e:
            log.error("%s: SAFETY GATE: %s", job.name, e)
            rc = max(rc, 3)
        except MigrationError as e:
            log.error("%s: %s", job.name, e)
            rc = max(rc, 1)
        finally:
            src.close()
            tgt.close()
    print(f"\nreports: {cfg.engine.report_dir}")
    return rc


def cmd_status(args, log) -> int:
    cfg = load_config(args.config)
    for st in StateStore(cfg.engine.state_dir).all():
        n = len(st.disks)
        done = sum(d.committed for d in st.disks.values())
        err = f"  last error: {st.errors[-1]['error']}" if st.errors else ""
        print(f"{st.name:24} {st.phase.value:16} disks {done}/{n} committed{err}")
    return 0


def cmd_cleanup(args, log) -> int:
    cfg = load_config(args.config)
    job = cfg.migration(args.migration)
    st = StateStore(cfg.engine.state_dir).load(job.name)
    if st is None:
        raise ConfigError(f"no state for {job.name}")
    ctx = _ctx(args, log)
    print(json.dumps(st.created, indent=2))
    if ctx.dry_run:
        print("\n(dry-run) the resources above would be deleted; pass --execute to delete them")
        return 0
    tgt = build_target(job.target, cfg.targets[job.target], ctx)
    tgt.cleanup_on_failure(st.created)
    if st.snapshot:
        from ..engine.pipeline import _snap
        build_source(job.source, cfg.sources[job.source], ctx).cleanup(_snap(st.snapshot))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vmigrate", description="any-to-any VM migration (dry-run by default)")
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--log-json", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *, cfg=True, execute=False):
        sp = sub.add_parser(name)
        if cfg:
            sp.add_argument("-c", "--config", required=True)
        if execute:
            sp.add_argument("--execute", action="store_true", help="actually perform changes")
        sp.set_defaults(fn=fn)
        return sp

    add("validate", cmd_validate)
    add("matrix", cmd_matrix, cfg=False)
    add("list-vms", cmd_list_vms).add_argument("--source", required=True)
    sp = add("inspect", cmd_inspect)
    sp.add_argument("--source", required=True)
    sp.add_argument("--vm", required=True)
    add("migrate", cmd_migrate, execute=True).add_argument("--only", nargs="*")
    add("status", cmd_status)
    add("cleanup", cmd_cleanup, execute=True).add_argument("--migration", required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log = setup_logging(args.log_level.upper(), args.log_json)
    try:
        return args.fn(args, log)
    except ConfigError as e:
        log.error("config: %s", e)
        return 2
    except SafetyGateError as e:
        log.error("SAFETY GATE: %s", e)
        return 3
    except MigrationError as e:
        log.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
