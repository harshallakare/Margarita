"""Purpose: the autoscaling worker supervisor. `vmigrate-platform worker` runs this by default: it
claims nothing itself, but starts one child process per job (`vmigrate-platform worker --once`) while
there is queued work this host can run, between a minimum and a maximum number of concurrent jobs, and
above the minimum only while the host has CPU, memory and staging space to spare.

Why child processes: every job gets its own Python interpreter, so the copy threads of two jobs no
longer compete for one interpreter lock (seen live: two disk copy threads in one worker process at
71% and 46% CPU, both stuck near 125 MB/s while the host was 73% idle). A crashed job takes only its
own process down; its lease expires and the conductor re-queues it as before.

Usage:
    Supervisor(services).run_forever()            # vmigrate-platform worker
    cfg = effective(repo_defaults, repo_host, cpu_count=8)
    ok, why = decide(cfg, running=1, starting=0, claimable=2, load=Load(cpu=40, mem_free=60, disk_free_gib=200),
                     since_spawn=60)

Settings (Settings page -> Workers, stored in platform_settings; the host value wins over the default):
    workers.autoscale            defaults for every host
    workers.host.<hostname>      overrides for one host
  mode: "auto" (the rules below decide) or "manual" (exactly `manual_slots` jobs at once, no load checks;
  the admin adds or removes workers up to the threshold `max_slots`).
  keys: min_slots (always allowed, regardless of load), max_slots (never more; null = automatic:
  half the CPUs, 1..4), cpu_max_pct, mem_min_free_pct, staging_min_free_gib, spawn_interval_s
  (wait between starts above the minimum, so the new job's load shows first), paused (drain: start
  no new jobs; running ones finish).

Not here: starting new worker MACHINES (that is worker pools with auto-provisioned minions, pools.py).
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("vmigrate.worker.autoscale")

KEY_DEFAULTS = "workers.autoscale"
KEY_HOST = "workers.host."
PARENT_ENV = "VMIGRATE_WORKER_PARENT"
STARTING_S = 15.0           # a child younger than this may not have claimed its job yet

MODES = ("auto", "manual")
DEFAULTS: dict[str, Any] = {"mode": "auto", "manual_slots": 1, "min_slots": 1, "max_slots": None, "cpu_max_pct": 80, "mem_min_free_pct": 15,
                            "staging_min_free_gib": 20, "spawn_interval_s": 30, "paused": False}
LIMITS: dict[str, tuple[float, float]] = {"manual_slots": (0, 64), "min_slots": (0, 64), "max_slots": (1, 64), "cpu_max_pct": (10, 100),
                                          "mem_min_free_pct": (0, 90), "staging_min_free_gib": (0, 100000),
                                          "spawn_interval_s": (0, 3600)}


def auto_max(cpu_count: int | None) -> int:
    return max(1, min(4, (cpu_count or 2) // 2))


def validate(values: Mapping[str, Any]) -> dict[str, Any]:
    """Clean a settings dict from the API (unknown keys refused; None = inherit)."""
    out: dict[str, Any] = {}
    for k, v in values.items():
        if k not in DEFAULTS:
            raise ValueError(f"unknown worker setting {k!r}")
        if v is None:
            out[k] = None
        elif k == "paused":
            out[k] = bool(v)
        elif k == "mode":
            if v not in MODES:
                raise ValueError("mode must be 'auto' or 'manual'")
            out[k] = v
        else:
            lo, hi = LIMITS[k]
            try:
                n = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be a number") from None
            if not lo <= n <= hi:
                raise ValueError(f"{k} must be between {lo:g} and {hi:g}")
            out[k] = int(n) if k in ("manual_slots", "min_slots", "max_slots", "spawn_interval_s") else n
    if out.get("min_slots") is not None and out.get("max_slots") is not None and out["min_slots"] > out["max_slots"]:
        raise ValueError("min_slots must be <= max_slots")
    if out.get("manual_slots") is not None and out.get("max_slots") is not None and out["manual_slots"] > out["max_slots"]:
        raise ValueError(f"manual workers ({out['manual_slots']}) cannot exceed the threshold ({out['max_slots']})")
    return out


def effective(defaults: Mapping[str, Any] | None, host: Mapping[str, Any] | None, *,
              cpu_count: int | None) -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    for layer in (defaults or {}, host or {}):
        cfg.update({k: v for k, v in layer.items() if k in DEFAULTS and v is not None})
    cfg["max_slots_auto"] = cfg["max_slots"] is None
    if cfg["max_slots"] is None:
        cfg["max_slots"] = auto_max(cpu_count)
    cfg["min_slots"] = min(int(cfg["min_slots"]), int(cfg["max_slots"]))
    cfg["manual_slots"] = min(int(cfg["manual_slots"]), int(cfg["max_slots"]))     # never above the threshold
    return cfg


@dataclass
class Load:
    cpu: float | None = None            # % busy over the last ~10 s
    mem_free: float | None = None       # % of RAM available
    disk_free_gib: float | None = None  # free space where jobs stage disks

    def to_dict(self) -> dict[str, Any]:
        return {"cpu_pct": None if self.cpu is None else round(self.cpu, 1),
                "mem_free_pct": None if self.mem_free is None else round(self.mem_free, 1),
                "staging_free_gib": None if self.disk_free_gib is None else round(self.disk_free_gib, 1)}


def decide(cfg: Mapping[str, Any], *, running: int, starting: int, claimable: int, load: Load,
           since_spawn: float) -> tuple[bool, str]:
    """Start one more job process now? (True, why) or (False, why not)."""
    if cfg.get("paused"):
        return False, "paused: starting no new jobs (running ones finish)"
    if cfg.get("mode") == "manual":
        n = int(cfg["manual_slots"])
        if running >= n:
            return False, f"manual: {n} worker(s) set, all busy" if n else "manual: 0 workers set"
        if claimable - starting <= 0:
            return False, f"manual: {n - running} of {n} worker(s) free, no queued job"
        return True, f"manual: {n - running} of {n} worker(s) free"
    if claimable - starting <= 0:
        return False, "no queued job for this host"
    if running >= cfg["max_slots"]:
        return False, f"at the maximum of {cfg['max_slots']} concurrent jobs"
    if running < cfg["min_slots"]:
        return True, f"below the minimum of {cfg['min_slots']}"
    if since_spawn < cfg["spawn_interval_s"]:
        return False, f"waiting {cfg['spawn_interval_s'] - since_spawn:.0f}s for the last job's load to show"
    if load.cpu is not None and load.cpu > cfg["cpu_max_pct"]:
        return False, f"CPU {load.cpu:.0f}% is above {cfg['cpu_max_pct']:g}%"
    if load.mem_free is not None and load.mem_free < cfg["mem_min_free_pct"]:
        return False, f"free memory {load.mem_free:.0f}% is below {cfg['mem_min_free_pct']:g}%"
    if load.disk_free_gib is not None and load.disk_free_gib < cfg["staging_min_free_gib"]:
        return False, f"staging space {load.disk_free_gib:.0f} GiB is below {cfg['staging_min_free_gib']:g} GiB"
    return True, "host has spare capacity"


class Sampler:
    """CPU (from /proc/stat over a sliding window), memory (/proc/meminfo) and staging free space.
    Without /proc (macOS dev) CPU falls back to the load average and memory is unknown."""

    def __init__(self, staging: Path | None, *, window: float = 10.0, proc: Path = Path("/proc"),
                 clock: Callable[[], float] = time.monotonic):
        self.staging, self.window, self.proc, self.clock = staging, window, proc, clock
        self.samples: deque[tuple[float, int, int]] = deque()

    def _cpu(self) -> float | None:
        try:
            first = (self.proc / "stat").read_text().splitlines()[0].split()
        except (OSError, IndexError):
            try:
                return min(100.0, os.getloadavg()[0] / (os.cpu_count() or 1) * 100)
            except (OSError, AttributeError):
                return None
        vals = [int(x) for x in first[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        now = self.clock()
        self.samples.append((now, sum(vals), idle))
        while len(self.samples) > 2 and now - self.samples[0][0] > self.window:
            self.samples.popleft()
        _, t0, i0 = self.samples[0]
        _, t1, i1 = self.samples[-1]
        return None if t1 == t0 else 100.0 * (1 - (i1 - i0) / (t1 - t0))

    def _mem(self) -> float | None:
        try:
            info = {ln.split(":")[0]: int(ln.split()[1]) for ln in (self.proc / "meminfo").read_text().splitlines()
                    if ":" in ln and len(ln.split()) >= 2}
            return 100.0 * info["MemAvailable"] / info["MemTotal"]
        except (OSError, KeyError, ValueError, ZeroDivisionError):
            return None

    def _disk(self) -> float | None:
        p = self.staging
        while p is not None and not p.exists():
            p = p.parent if p.parent != p else None
        if p is None:
            return None
        try:
            return shutil.disk_usage(p).free / (1 << 30)
        except OSError:
            return None

    def sample(self) -> Load:
        return Load(cpu=self._cpu(), mem_free=self._mem(), disk_free_gib=self._disk())


def child_argv(parent_id: str) -> list[str]:
    return [sys.executable, "-m", "vmigrate.cli.platform_cli", "worker", "--once"]


def _spawn(parent_id: str) -> subprocess.Popen:
    env = {**os.environ, PARENT_ENV: parent_id}
    return subprocess.Popen(child_argv(parent_id), env=env, stdin=subprocess.DEVNULL)


class Supervisor:
    def __init__(self, services: Any, *, spawn: Callable[[str], Any] = _spawn, sampler: Sampler | None = None,
                 clock: Callable[[], float] = time.monotonic, hostname: str | None = None,
                 region: str | None = None, capabilities: set[str] | None = None, cpu_count: int | None = None):
        from ..core.routing import detect_capabilities
        self.svc, self.repo = services, services.repo
        self.host = hostname or socket.gethostname()
        self.region = region if region is not None else (os.environ.get("VMIGRATE_WORKER_REGION") or None)
        self.capabilities = capabilities if capabilities is not None else detect_capabilities()
        self.id = f"{self.host}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.cpu_count = cpu_count if cpu_count is not None else os.cpu_count()
        staging = Path(os.environ.get("VMIGRATE_STAGING_DIR", "/var/lib/vmigrate/staging"))
        self.sampler = sampler or Sampler(staging if staging.exists() else Path(services.settings.data_dir))
        self._spawn, self.clock = spawn, clock
        self.children: list[tuple[Any, float]] = []      # (process, started at)
        self.last_spawn = -1e9
        self.started = 0
        self.last: dict[str, Any] = {}
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def config(self) -> dict[str, Any]:
        return effective(self.repo.get_setting(KEY_DEFAULTS), self.repo.get_setting(KEY_HOST + self.host),
                         cpu_count=self.cpu_count)

    def reap(self) -> None:
        keep = []
        for p, t in self.children:
            rc = p.poll()
            if rc is None:
                keep.append((p, t))
            elif rc != 0:
                log.warning("job process %s exited with %s", getattr(p, "pid", "?"), rc)
        self.children = keep

    def tick(self) -> dict[str, Any]:
        self.reap()
        now = self.clock()
        cfg = self.config()
        load = self.sampler.sample()
        running = len(self.children)
        starting = sum(1 for _, t in self.children if now - t < STARTING_S)
        claimable = self.repo.claimable_count(self.region, self.capabilities)
        ok, why = decide(cfg, running=running, starting=starting, claimable=claimable, load=load,
                         since_spawn=now - self.last_spawn)
        if ok:
            p = self._spawn(self.id)
            self.children.append((p, now))
            self.last_spawn, self.started = now, self.started + 1
            log.info("started job process %s (%s; %d running, %d queued for this host)",
                     getattr(p, "pid", "?"), why, running + 1, claimable)
            running += 1
        self.last = {"role": "supervisor", "running": running, "queued": claimable, "decision": why,
                     "mode": cfg["mode"], "manual_slots": cfg["manual_slots"],
                     "min_slots": cfg["min_slots"], "max_slots": cfg["max_slots"],
                     "max_slots_auto": cfg["max_slots_auto"], "paused": bool(cfg["paused"]),
                     "cpu_count": self.cpu_count, "started_total": self.started, **load.to_dict()}
        self.heartbeat()
        return self.last

    def heartbeat(self) -> None:
        self.repo.worker_heartbeat(self.id, self.host, os.getpid(), None, region=self.region,
                                   capabilities=sorted(self.capabilities), info=self.last or {"role": "supervisor"})

    def shutdown(self, timeout: float = 30.0) -> None:
        """Stop: forward SIGTERM to the job processes (their leases expire and the conductor re-queues the
        jobs, which resume) and wait for them."""
        for p, _ in self.children:
            try:
                p.send_signal(signal.SIGTERM)
            except (OSError, AttributeError):
                pass
        end = time.monotonic() + timeout
        for p, _ in self.children:
            try:
                p.wait(timeout=max(0.1, end - time.monotonic()))
            except Exception:  # noqa: BLE001
                try:
                    p.kill()
                except OSError:
                    pass
        self.children = []
        self.repo.remove_worker(self.id)

    def run_forever(self) -> None:
        log.info("worker supervisor %s started (%s CPUs)", self.id, self.cpu_count)
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, lambda *_: self._stop.set())
            except ValueError:          # not the main thread (tests)
                pass
        try:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:  # noqa: BLE001
                    log.exception("supervisor tick failed")
                self._stop.wait(self.svc.settings.poll_seconds)
        finally:
            self.shutdown()
