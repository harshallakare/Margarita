"""Purpose: relational schema for the platform (SQLAlchemy Core). The same tables run on Postgres
(production, docker-compose) and SQLite (dev, tests).

Usage:
    engine = make_engine("postgresql+psycopg://vmigrate:...@db/vmigrate")   # or sqlite:///./x.db
    metadata.create_all(engine)                                               # vmigrate-platform init-db

Tables: users, api_tokens, secrets, endpoints, migrations (also replicas: mode column),
executions (also the work queue: status + lease), tasks, task_logs, events, workers, schedules,
alerts + notification_channels (alerting: conductor evaluates rules, email goes out on fire/resolve).
upgrade_schema() adds columns introduced after a database was first created (no Alembic yet).
Secrets hold ONLY ciphertext; endpoint rows reference them by id.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine

metadata = MetaData()

users = Table(
    "users", metadata,
    Column("id", String(32), primary_key=True),
    Column("username", String(128), nullable=False, unique=True),
    Column("password_hash", String(256), nullable=False),
    Column("role", String(16), nullable=False),          # admin | operator | viewer
    Column("project", String(64), nullable=False, default="default"),
    Column("disabled", Boolean, nullable=False, default=False),
    Column("created_at", Float, nullable=False),
)

api_tokens = Table(
    "api_tokens", metadata,
    Column("id", String(32), primary_key=True),
    Column("user_id", String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("created_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
)

secrets = Table(
    "secrets", metadata,
    Column("id", String(32), primary_key=True),
    Column("ciphertext", Text, nullable=False),
    Column("created_at", Float, nullable=False),
)

endpoints = Table(
    "endpoints", metadata,
    Column("id", String(32), primary_key=True),
    Column("project", String(64), nullable=False),
    Column("name", String(128), nullable=False),
    Column("type", String(32), nullable=False),          # provider registry key: nutanix, vhi, ...
    Column("description", Text, nullable=False, default=""),
    Column("config", JSON, nullable=False),              # NON-secret provider config
    Column("secret_id", String(32), ForeignKey("secrets.id"), nullable=True),
    Column("region", String(64), nullable=True),           # site/region for worker routing (optional)
    Column("required_tags", JSON, nullable=True),          # extra worker capabilities this endpoint needs
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint("project", "name"),
)

migrations = Table(
    "migrations", metadata,
    Column("id", String(32), primary_key=True),
    Column("project", String(64), nullable=False),
    Column("name", String(128), nullable=False),
    Column("mode", String(16), nullable=False, default="migration"),   # migration | replica
    Column("source_endpoint_id", String(32), ForeignKey("endpoints.id"), nullable=False),
    Column("target_endpoint_id", String(32), ForeignKey("endpoints.id"), nullable=False),
    Column("vm", String(256), nullable=False),
    Column("options", JSON, nullable=False),             # target_name, guest_os, target_options, osmorph, engine
    Column("state", JSON, nullable=True),                # engine MigrationState (resume point)
    Column("created_by", String(32), nullable=True),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint("project", "name"),
)

executions = Table(
    "executions", metadata,
    Column("id", String(32), primary_key=True),
    Column("migration_id", String(32), ForeignKey("migrations.id", ondelete="CASCADE"), nullable=False),
    Column("kind", String(32), nullable=False),          # migration | replica_sync | replica_deploy | replica_delete | replica_cutover
    Column("params", JSON, nullable=True),               # kind-specific (deploy: restore_point, deploy_name, ...)
    Column("region", String(64), nullable=True),           # routing: only workers in this region claim it
    Column("required_caps", JSON, nullable=True),          # routing: {"region", "caps": [...], "cross_site"}
    Column("waiting_reason", Text, nullable=True),         # why no worker has claimed it (conductor)
    Column("dry_run", Boolean, nullable=False),
    Column("status", String(16), nullable=False),        # ExecStatus
    Column("error", Text, nullable=True),
    Column("cancel_requested", Boolean, nullable=False, default=False),
    Column("lease_owner", String(128), nullable=True),
    Column("lease_expires", Float, nullable=True),
    Column("runs", Integer, nullable=False, default=0),  # how many times a worker picked it up
    Column("created_by", String(32), nullable=True),
    Column("created_at", Float, nullable=False),
    Column("started_at", Float, nullable=True),
    Column("finished_at", Float, nullable=True),
)
Index("ix_executions_status", executions.c.status)

tasks = Table(
    "tasks", metadata,
    Column("execution_id", String(32), ForeignKey("executions.id", ondelete="CASCADE"), primary_key=True),
    Column("id", String(200), primary_key=True),
    Column("kind", String(64), nullable=False),
    Column("label", String(256), nullable=False, default=""),
    Column("seq", Integer, nullable=False),
    Column("depends_on", JSON, nullable=False),
    Column("params", JSON, nullable=False),
    Column("max_attempts", Integer, nullable=False, default=1),
    Column("status", String(16), nullable=False),
    Column("attempts", Integer, nullable=False, default=0),
    Column("progress", Float, nullable=False, default=0.0),
    Column("message", Text, nullable=False, default=""),
    Column("error", Text, nullable=True),
    Column("result", JSON, nullable=False),
    Column("started_at", Float, nullable=True),
    Column("finished_at", Float, nullable=True),
)

task_logs = Table(
    "task_logs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("execution_id", String(32), ForeignKey("executions.id", ondelete="CASCADE"), nullable=False),
    Column("task_id", String(200), nullable=True),
    Column("ts", Float, nullable=False),
    Column("level", String(10), nullable=False),
    Column("message", Text, nullable=False),
    Column("logger", String(128), nullable=True),
)
Index("ix_task_logs_exec", task_logs.c.execution_id, task_logs.c.id)
Index("ix_task_logs_ts", task_logs.c.ts)

audit_log = Table(
    "audit_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", Float, nullable=False),
    Column("actor", String(128), nullable=False),        # username, "system:conductor", or "anonymous"
    Column("actor_id", String(32), nullable=True),
    Column("actor_role", String(16), nullable=True),
    Column("ip", String(64), nullable=True),
    Column("user_agent", String(256), nullable=True),
    Column("action", String(64), nullable=False),        # e.g. endpoint.create, replica.cutover, auth.login
    Column("object_type", String(32), nullable=True),
    Column("object_id", String(128), nullable=True),
    Column("object_name", String(256), nullable=True),
    Column("project", String(64), nullable=True),
    Column("method", String(8), nullable=True),
    Column("path", String(512), nullable=True),
    Column("status", Integer, nullable=True),
    Column("outcome", String(16), nullable=False),       # success | denied | failed
    Column("request_id", String(64), nullable=True),
    Column("details", JSON, nullable=True),              # redacted request body / context
    Column("prev_hash", String(64), nullable=False),
    Column("hash", String(64), nullable=False),
)
Index("ix_audit_ts", audit_log.c.ts)
Index("ix_audit_object", audit_log.c.object_type, audit_log.c.object_id)

audit_head = Table(                                      # one row: the chain head (optimistic append lock)
    "audit_head", metadata,
    Column("id", Integer, primary_key=True),
    Column("hash", String(64), nullable=False),
    Column("last_id", Integer, nullable=False),
)

events = Table(
    "events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("execution_id", String(32), nullable=True),
    Column("migration_id", String(32), nullable=True),
    Column("ts", Float, nullable=False),
    Column("type", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index("ix_events_exec", events.c.execution_id, events.c.id)

schedules = Table(
    "schedules", metadata,
    Column("id", String(32), primary_key=True),
    Column("migration_id", String(32), ForeignKey("migrations.id", ondelete="CASCADE"), nullable=False),
    Column("cron", String(128), nullable=False),          # 5-field cron, UTC
    Column("enabled", Boolean, nullable=False, default=True),
    Column("next_run_at", Float, nullable=True),
    Column("last_run_at", Float, nullable=True),
    Column("last_execution_id", String(32), nullable=True),
    Column("last_skip_reason", Text, nullable=True),
    Column("created_at", Float, nullable=False),
)
Index("ix_schedules_due", schedules.c.enabled, schedules.c.next_run_at)

workers = Table(
    "workers", metadata,
    Column("id", String(128), primary_key=True),
    Column("hostname", String(256), nullable=False),
    Column("pid", Integer, nullable=False),
    Column("started_at", Float, nullable=False),
    Column("heartbeat_at", Float, nullable=False),
    Column("current_execution", String(32), nullable=True),
    Column("region", String(64), nullable=True),
    Column("capabilities", JSON, nullable=True),
    Column("export_url", String(512), nullable=True),      # export agents: https URL other sites call
    Column("export_fp", String(128), nullable=True),       # export agents: pinned TLS cert SHA-256
)


pools = Table(
    "pools", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(128), nullable=False, unique=True),
    Column("region", String(64), nullable=True),
    Column("description", Text, nullable=True),
    Column("min_size", Integer, nullable=False, default=0),
    Column("max_size", Integer, nullable=False, default=0),
    Column("provision", JSON, nullable=True),     # auto-provisioned minions: {endpoint_id, image, flavor, network, api_url, ...}
    Column("state", JSON, nullable=True),         # {minions: [...], last_error, last_scale_at}
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)

shared_disks = Table(
    "shared_disks", metadata,
    Column("cluster", String(128), primary_key=True),
    Column("shared_id", String(256), primary_key=True),
    Column("owner", String(32), nullable=False),        # migration id that copies it
    Column("status", String(16), nullable=False),       # claimed | created | done | failed
    Column("target", JSON, nullable=True),
    Column("size_bytes", Integer, nullable=True),
    Column("updated_at", Float, nullable=False),
)

replica_hashes = Table(                  # SHA-256 per 4 MiB block of each replica disk, as last written
    "replica_hashes", metadata,
    Column("migration_id", String(32), primary_key=True),
    Column("disk_id", String(256), primary_key=True),
    Column("block_size", Integer, nullable=False),
    Column("data", LargeBinary, nullable=False),
    Column("updated_at", Float, nullable=False),
)

alerts = Table(                          # one row per firing episode; resolved rows are history
    "alerts", metadata,
    Column("id", String(32), primary_key=True),
    Column("key", String(256), nullable=False),          # rule identity, e.g. "rpo:<replica id>"
    Column("kind", String(64), nullable=False),
    Column("severity", String(16), nullable=False),      # critical | warning
    Column("status", String(16), nullable=False),        # firing | resolved
    Column("project", String(128), nullable=True),
    Column("migration_id", String(32), nullable=True),
    Column("execution_id", String(32), nullable=True),
    Column("object_name", String(256), nullable=True),
    Column("title", Text, nullable=False),
    Column("detail", Text, nullable=True),
    Column("first_at", Float, nullable=False),
    Column("last_at", Float, nullable=False),
    Column("resolved_at", Float, nullable=True),
    Column("notified_at", Float, nullable=True),
    Column("ack_by", String(128), nullable=True),
    Column("ack_at", Float, nullable=True),
)
Index("ix_alerts_status", alerts.c.status, alerts.c.key)

notification_channels = Table(
    "notification_channels", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(128), nullable=False, unique=True),
    Column("type", String(32), nullable=False),          # email
    Column("config", JSON, nullable=False),              # host, port, security, username, from_addr, to, ...
    Column("secret_id", String(32), nullable=True),      # SMTP password (secret store, ciphertext only)
    Column("enabled", Boolean, nullable=False, default=True),
    Column("min_severity", String(16), nullable=False, default="warning"),
    Column("notify_resolved", Boolean, nullable=False, default=True),
    Column("notify_success", Boolean, nullable=False, default=False),
    Column("last_sent_at", Float, nullable=True),
    Column("last_error", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)

enroll_tokens = Table(
    "enroll_tokens", metadata,
    Column("id", String(32), primary_key=True),
    Column("token_hash", String(128), nullable=False, unique=True),
    Column("pool_id", String(32), nullable=True),
    Column("expires_at", Float, nullable=False),
    Column("used_at", Float, nullable=True),
    Column("used_by", String(256), nullable=True),
    Column("created_by", String(32), nullable=True),
    Column("created_at", Float, nullable=False),
)


# columns added after the first release: (table, column, DDL type)
_ADDED_COLUMNS = [("executions", "params", "JSON"), ("executions", "region", "VARCHAR(64)"),
                  ("executions", "required_caps", "JSON"), ("executions", "waiting_reason", "TEXT"),
                  ("endpoints", "region", "VARCHAR(64)"), ("endpoints", "required_tags", "JSON"),
                  ("workers", "region", "VARCHAR(64)"), ("workers", "capabilities", "JSON"),
                  ("workers", "export_url", "VARCHAR(512)"), ("workers", "export_fp", "VARCHAR(128)"),
                  ("task_logs", "logger", "VARCHAR(128)")]


def upgrade_schema(engine: Engine) -> list[str]:
    """Create missing tables and add missing columns in place. Returns what was added."""
    from sqlalchemy import inspect, text
    metadata.create_all(engine)
    added = []
    insp = inspect(engine)
    for table, col, ddl in _ADDED_COLUMNS:
        if col not in {c["name"] for c in insp.get_columns(table)}:
            with engine.begin() as c:
                c.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
            added.append(f"{table}.{col}")
    return added


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        eng = create_engine(url, connect_args={"timeout": 30, "check_same_thread": False})

        @event.listens_for(eng, "connect")
        def _pragmas(dbapi_conn, _rec):  # WAL so API, conductor and workers can share one file in dev
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()
        return eng
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20)
