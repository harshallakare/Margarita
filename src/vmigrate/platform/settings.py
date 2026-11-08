"""Purpose: process settings for the platform services (API, conductor, worker), read from the
environment only, so containers configure them without files. Secrets never have defaults.

Usage:
    st = Settings.from_env()
    Services.from_settings(st)       # -> repo, secret store, engine config (see factory.py)

Environment:
    VMIGRATE_DATABASE_URL       default sqlite:///./vmigrate-platform.db
                                (postgresql+psycopg://user:pass@host/db in production)
    VMIGRATE_SECRET_KEY(_FILE)  Fernet key(s) for the secret store (REQUIRED)
    VMIGRATE_ENGINE_CONFIG      optional YAML with an `engine:` block (EngineConfig defaults)
    VMIGRATE_DATA_DIR           default ./vmigrate-data (reports; fake-target staging in demos)
    VMIGRATE_LEASE_SECONDS      default 60 (worker lease; a crashed worker's execution is re-queued after it)
    VMIGRATE_POLL_SECONDS       default 2 (conductor + worker loop interval)
    VMIGRATE_TOKEN_TTL_HOURS    default 12
    VMIGRATE_ALLOWED_PROVIDERS  optional comma list restricting endpoint types (e.g. "nutanix,vhi")
    VMIGRATE_CORS_ORIGINS       optional comma list for the web UI dev server (e.g. http://localhost:5173)
    VMIGRATE_WEB_DIR            built web UI to serve at /ui (default ./web/dist when it exists)
    VMIGRATE_PUBLIC_URL         optional external URL of the UI/API (https://margarita.example.com); used for
                                links in alert emails
    VMIGRATE_ALERT_REPEAT_HOURS default 24: re-send unacknowledged critical alerts this often (0 = never)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    database_url: str = "sqlite:///./vmigrate-platform.db"
    secret_key: str | None = None
    engine_config: Path | None = None
    data_dir: Path = Path("./vmigrate-data")
    lease_seconds: float = 60.0
    poll_seconds: float = 2.0
    token_ttl_hours: float = 12.0
    allowed_providers: list[str] = field(default_factory=list)
    cors_origins: list[str] = field(default_factory=list)
    web_dir: Path | None = None
    allow_http_enroll: bool = False     # VMIGRATE_ALLOW_HTTP_ENROLL=1: lab only (secrets cross the wire in clear)
    public_url: str | None = None
    alert_repeat_hours: float = 24.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = dict(os.environ) if env is None else env
        from ..security.secretstore import load_key

        def lst(k: str) -> list[str]:
            return [x.strip() for x in e.get(k, "").split(",") if x.strip()]

        return cls(
            allow_http_enroll=e.get("VMIGRATE_ALLOW_HTTP_ENROLL", "") in ("1", "true", "yes"),
            database_url=e.get("VMIGRATE_DATABASE_URL", cls.database_url),
            secret_key=load_key(e),
            engine_config=Path(e["VMIGRATE_ENGINE_CONFIG"]) if e.get("VMIGRATE_ENGINE_CONFIG") else None,
            data_dir=Path(e.get("VMIGRATE_DATA_DIR", "./vmigrate-data")),
            lease_seconds=float(e.get("VMIGRATE_LEASE_SECONDS", 60)),
            poll_seconds=float(e.get("VMIGRATE_POLL_SECONDS", 2)),
            token_ttl_hours=float(e.get("VMIGRATE_TOKEN_TTL_HOURS", 12)),
            allowed_providers=lst("VMIGRATE_ALLOWED_PROVIDERS"),
            cors_origins=lst("VMIGRATE_CORS_ORIGINS"),
            web_dir=Path(e.get("VMIGRATE_WEB_DIR", "web/dist")),
            public_url=e.get("VMIGRATE_PUBLIC_URL") or None,
            alert_repeat_hours=float(e.get("VMIGRATE_ALERT_REPEAT_HOURS", 24)),
        )
