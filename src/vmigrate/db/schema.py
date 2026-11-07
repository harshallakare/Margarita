"""Purpose: relational schema for the platform (SQLAlchemy Core). The same tables run on Postgres
(production, docker-compose) and SQLite (dev, tests).

Usage:
    engine = make_engine("postgresql+psycopg://vmigrate:...@db/vmigrate")   # or sqlite:///./x.db
    metadata.create_all(engine)                                               # vmigrate-platform init-db

Tables: users, api_tokens, secrets, endpoints, migrations (also replicas: mode column),
executions (also the work queue: status + lease), tasks, task_logs, events, workers, schedules.
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
    Column("kind", String(32), nullable=False),          # migration | replica_sync | replica_deploy | replica_delete
    Column("params", JSON, nullable=True),               # kind-specific (deploy: restore_point, deploy_name, ...)
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
)
Index("ix_task_logs_exec", task_logs.c.execution_id, task_logs.c.id)

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
)


# columns added after the first release: (table, column, DDL type)
_ADDED_COLUMNS = [("executions", "params", "JSON")]


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
