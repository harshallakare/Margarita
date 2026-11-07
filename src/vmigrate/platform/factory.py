"""Purpose: the one place that turns DB rows into live objects: endpoint row (+ decrypted secret)
-> provider instance, migration row -> MigrationConfig + EngineConfig + MigrationRunner. API,
conductor and worker all go through Services so they agree on these mappings.

Usage:
    svc = Services.from_settings(Settings.from_env())
    src = svc.build_source(endpoint_row, ctx)
    runner = svc.build_runner(migration_row, ctx)

Migration `options` (JSON, all optional):
    target_name, guest_os{family,distro,version,bits}, app_consistent,
    target_options{flavor, network_map{src-net: dst-net}, storage_pool, availability_zone, boot_bus, ...},
    osmorph{fstab_harden, disable_periodic_fsck, fstab_uuid_map, allow_manual},
    retention (replicas only: restore points to keep, default 3),
    engine{max_parallel_disks, chunk_size, checkpoint_every, verify_samples, verify_sample_bytes,
           app_consistent, cleanup_source_snapshot, on_failure, boot_check, boot_timeout}
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import yaml

from ..config.loader import EngineConfig, MigrationConfig, _engine
from ..core.context import RunContext
from ..core.errors import ConfigError
from ..core.interfaces import SourceProvider, TargetProvider
from ..db.repo import DbReplicaStore, DbStateStore, Repo
from ..db.schema import make_engine
from ..engine.pipeline import MigrationRunner
from ..engine.replica import ReplicaRunner
from ..providers import build_source, build_target
from ..security.secretstore import FernetSecretStore, SecretStore
from .settings import Settings

ENGINE_OVERRIDES = {"max_parallel_disks", "chunk_size", "checkpoint_every", "verify_samples", "verify_sample_bytes",
                    "app_consistent", "cleanup_source_snapshot", "on_failure", "boot_check", "boot_timeout"}


@dataclass
class Services:
    settings: Settings
    repo: Repo
    secrets: SecretStore
    engine_base: dict[str, Any]

    @classmethod
    def from_settings(cls, st: Settings, *, init_schema: bool = True) -> Services:
        eng = make_engine(st.database_url)
        repo = Repo(eng)
        if init_schema:
            repo.init_schema()
        if not st.secret_key:
            raise ConfigError("secret key not configured")
        base: dict[str, Any] = {}
        if st.engine_config:
            base = (yaml.safe_load(st.engine_config.read_text()) or {}).get("engine", {}) or {}
        base.pop("state_dir", None)
        base.setdefault("report_dir", str(st.data_dir / "reports"))
        _engine(dict(base))  # validate once at startup
        return cls(settings=st, repo=repo, secrets=FernetSecretStore(eng, st.secret_key), engine_base=base)

    # ------------------------------------------------------------------ providers
    def provider_config(self, ep: dict[str, Any]) -> dict[str, Any]:
        creds = self.secrets.get(ep["secret_id"]) if ep.get("secret_id") else {}
        return {**ep["config"], **creds, "type": ep["type"]}

    def build_source(self, ep: dict[str, Any], ctx: RunContext) -> SourceProvider:
        return build_source(ep["name"], self.provider_config(ep), ctx)

    def build_target(self, ep: dict[str, Any], ctx: RunContext) -> TargetProvider:
        return build_target(ep["name"], self.provider_config(ep), ctx)

    # ------------------------------------------------------------------ migrations
    def engine_for(self, mig: dict[str, Any]) -> EngineConfig:
        over = dict((mig.get("options") or {}).get("engine") or {})
        bad = set(over) - ENGINE_OVERRIDES
        if bad:
            raise ConfigError(f"options.engine: keys not overridable per migration: {sorted(bad)}")
        e = _engine({**self.engine_base, **over, "state_dir": str(self.settings.data_dir / "state")})
        return e

    def job_for(self, mig: dict[str, Any]) -> MigrationConfig:
        o = mig.get("options") or {}
        return MigrationConfig(
            name=mig["name"], source=mig["source_endpoint_id"], target=mig["target_endpoint_id"], vm=mig["vm"],
            target_name=o.get("target_name"), guest_os=o.get("guest_os"),
            target_options=dict(o.get("target_options") or {}), osmorph=dict(o.get("osmorph") or {}),
            app_consistent=o.get("app_consistent"),
        )

    def _endpoints(self, mig: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        src_ep = self.repo.get_endpoint(mig["source_endpoint_id"])
        tgt_ep = self.repo.get_endpoint(mig["target_endpoint_id"])
        if src_ep is None or tgt_ep is None:
            raise ConfigError(f"{mig['name']}: source or target endpoint no longer exists")
        return src_ep, tgt_ep

    def build_runner(self, mig: dict[str, Any], ctx: RunContext) -> MigrationRunner:
        src_ep, tgt_ep = self._endpoints(mig)
        return MigrationRunner(self.job_for(mig), self.engine_for(mig), self.build_source(src_ep, ctx),
                               self.build_target(tgt_ep, ctx), ctx, DbStateStore(self.repo, mig["id"]))

    def build_replica_runner(self, mig: dict[str, Any], ctx: RunContext) -> ReplicaRunner:
        src_ep, tgt_ep = self._endpoints(mig)
        return ReplicaRunner(self.job_for(mig), self.engine_for(mig), self.build_source(src_ep, ctx),
                             self.build_target(tgt_ep, ctx), ctx, DbReplicaStore(self.repo, mig["id"]),
                             retention=int((mig.get("options") or {}).get("retention", 3)))


def engine_dict(e: EngineConfig) -> dict[str, Any]:
    return {k: (str(v) if k.endswith("_dir") else v) for k, v in asdict(e).items()}
