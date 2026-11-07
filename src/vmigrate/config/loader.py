"""Purpose: load + validate the YAML config. Secrets come from the environment via ${VAR}
interpolation (or a *_env key the provider resolves) — never from code.

Usage:
    cfg = load_config("migrate.yaml")
    cfg.engine.max_parallel_disks; cfg.sources["ahv1"]; cfg.migrations[0].vm

Schema (see examples/config.example.yaml):
  engine:      state_dir, report_dir, max_parallel_disks, chunk_size, checkpoint_every,
               verify_samples, verify_sample_bytes, app_consistent, cleanup_source_snapshot,
               on_failure (keep|cleanup), boot_check, boot_timeout
  sources:     {name: {type: <provider>, ...provider config}}
  targets:     {name: {type: <provider>, ...provider config}}
  migrations:  [{name, source, target, vm, target_name?, guest_os?, target_options?, osmorph?}]
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..core.errors import ConfigError

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]i?B?|B)?\s*$", re.I)
_MULT = {"": 1, "B": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
SECRET_KEYS = {"password", "secret", "token", "api_key", "client_secret"}


def parse_size(v: Any) -> int:
    if isinstance(v, int):
        return v
    m = _SIZE.match(str(v))
    if not m:
        raise ConfigError(f"invalid size {v!r} (use e.g. 64MiB, 1GiB, 1048576)")
    unit = (m.group(2) or "").upper()[:1] if (m.group(2) or "").upper() != "B" else "B"
    return int(float(m.group(1)) * _MULT[unit])


def interpolate(obj: Any, env: dict[str, str] | None = None, path: str = "") -> Any:
    env = dict(os.environ) if env is None else env
    if isinstance(obj, dict):
        return {k: interpolate(v, env, f"{path}.{k}") for k, v in obj.items()}
    if isinstance(obj, list):
        return [interpolate(v, env, f"{path}[{i}]") for i, v in enumerate(obj)]
    if isinstance(obj, str):
        def rep(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            if name in env:
                return env[name]
            if default is not None:
                return default
            raise ConfigError(f"{path.lstrip('.')}: environment variable ${{{name}}} is not set")
        return _ENV.sub(rep, obj)
    return obj


def find_literal_secrets(raw: Any, path: str = "") -> list[str]:
    """Paths of secret-looking keys whose raw YAML value is a literal (not ${VAR})."""
    hits: list[str] = []
    if isinstance(raw, dict):
        for k, v in raw.items():
            p = f"{path}.{k}".lstrip(".")
            if k.lower() in SECRET_KEYS and isinstance(v, str) and v and not _ENV.fullmatch(v.strip()):
                hits.append(p)
            hits += find_literal_secrets(v, p)
    elif isinstance(raw, list):
        for i, v in enumerate(raw):
            hits += find_literal_secrets(v, f"{path}[{i}]")
    return hits


@dataclass
class EngineConfig:
    state_dir: Path = Path("./vmigrate-state")
    report_dir: Path = Path("./vmigrate-state/reports")
    max_parallel_disks: int = 4
    chunk_size: int = 64 << 20
    checkpoint_every: int = 1 << 30
    verify_samples: int = 32
    verify_sample_bytes: int = 1 << 20
    app_consistent: bool = True
    cleanup_source_snapshot: bool = True
    on_failure: str = "keep"     # keep (resume later) | cleanup (delete what this run created on target)
    boot_check: bool = True
    boot_timeout: int = 900


@dataclass
class MigrationConfig:
    name: str
    source: str
    target: str
    vm: str
    target_name: str | None = None
    guest_os: dict[str, Any] | None = None
    target_options: dict[str, Any] = field(default_factory=dict)
    osmorph: dict[str, Any] = field(default_factory=dict)
    app_consistent: bool | None = None


@dataclass
class Config:
    engine: EngineConfig
    sources: dict[str, dict[str, Any]]
    targets: dict[str, dict[str, Any]]
    migrations: list[MigrationConfig]
    warnings: list[str] = field(default_factory=list)

    def migration(self, name: str) -> MigrationConfig:
        for m in self.migrations:
            if m.name == name:
                return m
        raise ConfigError(f"no migration named {name!r}; have {[m.name for m in self.migrations]}")


def _engine(d: dict[str, Any]) -> EngineConfig:
    e = EngineConfig()
    known = set(EngineConfig.__dataclass_fields__)
    unknown = set(d) - known
    if unknown:
        raise ConfigError(f"engine: unknown keys {sorted(unknown)}")
    for k, v in d.items():
        if k in ("state_dir", "report_dir"):
            v = Path(v)
        elif k in ("chunk_size", "checkpoint_every", "verify_sample_bytes"):
            v = parse_size(v)
        setattr(e, k, v)
    if "report_dir" not in d:
        e.report_dir = e.state_dir / "reports"
    if e.on_failure not in ("keep", "cleanup"):
        raise ConfigError("engine.on_failure must be 'keep' or 'cleanup'")
    if not 1 <= int(e.max_parallel_disks) <= 64:
        raise ConfigError("engine.max_parallel_disks must be 1..64")
    if e.chunk_size < (1 << 20) or e.chunk_size % 4096:
        raise ConfigError("engine.chunk_size must be >= 1MiB and 4KiB aligned")
    return e


def build_config(raw: dict[str, Any], env: dict[str, str] | None = None) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    warnings = [f"{p}: literal secret in config file — prefer ${{ENV_VAR}}" for p in find_literal_secrets(raw)]
    data = interpolate(raw, env)
    unknown = set(data) - {"engine", "sources", "targets", "migrations"}
    if unknown:
        raise ConfigError(f"unknown top-level keys {sorted(unknown)}")
    sources = data.get("sources") or {}
    targets = data.get("targets") or {}
    for kind, group in (("sources", sources), ("targets", targets)):
        if not isinstance(group, dict):
            raise ConfigError(f"{kind} must be a mapping of name -> provider config")
        for n, c in group.items():
            if not isinstance(c, dict) or "type" not in c:
                raise ConfigError(f"{kind}.{n}: missing 'type'")
    migs: list[MigrationConfig] = []
    seen: set[str] = set()
    for i, m in enumerate(data.get("migrations") or []):
        missing = {"name", "source", "target", "vm"} - set(m)
        if missing:
            raise ConfigError(f"migrations[{i}]: missing {sorted(missing)}")
        extra = set(m) - set(MigrationConfig.__dataclass_fields__)
        if extra:
            raise ConfigError(f"migrations[{i}] ({m.get('name')}): unknown keys {sorted(extra)}")
        if m["name"] in seen:
            raise ConfigError(f"duplicate migration name {m['name']!r}")
        seen.add(m["name"])
        if m["source"] not in sources:
            raise ConfigError(f"migrations[{i}]: unknown source {m['source']!r}")
        if m["target"] not in targets:
            raise ConfigError(f"migrations[{i}]: unknown target {m['target']!r}")
        migs.append(MigrationConfig(**m))
    return Config(engine=_engine(data.get("engine") or {}), sources=sources, targets=targets,
                  migrations=migs, warnings=warnings)


def load_config(path: str | os.PathLike[str], env: dict[str, str] | None = None) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"{p}: invalid YAML: {e}") from e
    return build_config(raw or {}, env)
