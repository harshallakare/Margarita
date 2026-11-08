"""Purpose: request/response models for the REST API (the contract the CLI and the web UI share).
Credentials are write-only: they appear in Create/Update bodies and never in any response.

Usage: imported by vmigrate.api.app; OpenAPI is served at /docs and /openapi.json.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"


class TokenRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    token: str
    expires_in: int
    username: str
    role: str
    project: str


class UserCreate(BaseModel):
    username: str = Field(pattern=NAME)
    password: str = Field(min_length=12)
    role: Literal["viewer", "operator", "admin"] = "operator"
    project: str = "default"


class PoolProvision(BaseModel):
    endpoint_id: str = Field(..., description="VHI/OpenStack endpoint whose project hosts the minions")
    image: str
    flavor: str
    network: str
    api_url: str = Field(..., description="HTTPS URL of this Margarita API, as the minions reach it")
    key_name: str | None = None
    security_groups: list[str] | None = None
    repo: str | None = None
    branch: str | None = None
    full: bool = False


class PoolCreate(BaseModel):
    name: str = Field(..., pattern=NAME)
    region: str | None = None
    description: str | None = None
    min_size: int = Field(0, ge=0, le=100)
    max_size: int = Field(0, ge=0, le=100)
    provision: PoolProvision | None = None


class PoolUpdate(BaseModel):
    region: str | None = None
    description: str | None = None
    min_size: int | None = Field(None, ge=0, le=100)
    max_size: int | None = Field(None, ge=0, le=100)
    provision: PoolProvision | None = None


class EnrollTokenRequest(BaseModel):
    ttl_minutes: int = Field(60, ge=5, le=24 * 60)


class EnrollRequest(BaseModel):
    token: str
    hostname: str = ""


class EndpointCreate(BaseModel):
    name: str = Field(pattern=NAME)
    type: str
    description: str = ""
    config: dict[str, Any] = Field(default_factory=dict, description="non-secret provider config")
    credentials: dict[str, Any] = Field(default_factory=dict, description="secret fields; stored encrypted, never returned")
    region: str | None = Field(None, pattern=r"^[A-Za-z0-9._-]{1,64}$", description="site/region for worker routing")
    required_tags: list[str] = Field(default_factory=list, description="worker capabilities this endpoint needs")


class EndpointDiscover(BaseModel):
    """Settings as typed in the endpoint form (nothing saved). With endpoint_id, stored credentials
    fill any secret left empty, so an edit needs no password re-entry."""
    type: str
    config: dict[str, Any] = Field(default_factory=dict)
    credentials: dict[str, Any] = Field(default_factory=dict)
    endpoint_id: str | None = None


class EndpointUpdate(BaseModel):
    description: str | None = None
    config: dict[str, Any] | None = None
    credentials: dict[str, Any] | None = None
    region: str | None = Field(None, pattern=r"^[A-Za-z0-9._-]{0,64}$")
    required_tags: list[str] | None = None


class EndpointOut(BaseModel):
    id: str
    name: str
    type: str
    project: str
    description: str
    config: dict[str, Any]
    has_credentials: bool
    credential_keys: list[str]
    region: str | None = None
    required_tags: list[str] = Field(default_factory=list)
    created_at: float
    updated_at: float


class MigrationOptions(BaseModel):
    target_name: str | None = None
    guest_os: dict[str, Any] | None = None
    app_consistent: bool | None = None
    target_options: dict[str, Any] = Field(default_factory=dict,
                                           description="flavor, network_map{src: dst}, storage_pool, availability_zone, boot_bus, ...")
    osmorph: dict[str, Any] = Field(default_factory=dict)
    engine: dict[str, Any] = Field(default_factory=dict)
    retention: int | None = Field(None, ge=1, le=100, description="replicas: restore points to keep (default 3)")
    pool: str | None = Field(None, description="run only on workers of this worker pool (tag pool:<name>)")
    cluster: str | None = Field(None, pattern=NAME, description="cluster group: VMs sharing disks; each shared disk is "
                                "copied once and attached to every member")


class MigrationCreate(BaseModel):
    name: str = Field(pattern=NAME)
    source_endpoint_id: str
    target_endpoint_id: str
    vm: str
    options: MigrationOptions = Field(default_factory=MigrationOptions)


class MigrationUpdate(BaseModel):
    options: MigrationOptions


class StartRequest(BaseModel):
    dry_run: bool = Field(True, description="plan + validate only (the default). Set false to change things.")


class ReplicaCreate(MigrationCreate):
    schedule: str | None = Field(None, description="optional cron (UTC) for automatic syncs, e.g. '0 */4 * * *'")


class CutoverRequest(BaseModel):
    dry_run: bool = True
    shutdown_source: bool = False         # opt-in: the only time Margarita changes the source VM's state
    force_power_off: bool = False         # hard power-off instead of a guest shutdown
    shutdown_timeout: int = Field(900, ge=30, le=7200)
    deploy_name: str | None = None        # default: the VM's name (a cutover takes over its identity)


class DeployRequest(BaseModel):
    dry_run: bool = True
    restore_point: str | None = Field(None, description="restore point id; default = latest completed sync")
    deploy_name: str | None = Field(None, pattern=NAME, description="VM name on the target (default <vm>-dr)")


class ScheduleCreate(BaseModel):
    cron: str
    enabled: bool = True


class ScheduleUpdate(BaseModel):
    cron: str | None = None
    enabled: bool | None = None


class ScheduleOut(BaseModel):
    id: str
    replica_id: str
    cron: str
    enabled: bool
    next_run_at: float | None
    last_run_at: float | None
    last_execution_id: str | None
    last_skip_reason: str | None
    upcoming: list[float] = Field(default_factory=list)


class ReplicaOut(BaseModel):
    id: str
    name: str
    project: str
    source_endpoint_id: str
    target_endpoint_id: str
    vm: str
    options: dict[str, Any]
    disks: int | None
    disk_total_bytes: int | None
    crosscheck: dict[str, int] | None
    consistent: bool
    current_sync: dict[str, Any] | None
    last_sync: dict[str, Any] | None
    syncs: list[dict[str, Any]]
    restore_points: list[dict[str, Any]]
    deployments: list[dict[str, Any]]
    cutovers: list[dict[str, Any]] = []
    errors: list[dict[str, Any]]
    schedules: list[ScheduleOut]
    created_at: float
    updated_at: float
    last_execution: ExecutionOut | None = None


class ExecutionOut(BaseModel):
    id: str
    migration_id: str
    kind: str
    dry_run: bool
    params: dict[str, Any] | None = None
    region: str | None = None
    waiting_reason: str | None = None
    status: str
    error: str | None
    cancel_requested: bool
    lease_owner: str | None
    runs: int
    created_at: float
    started_at: float | None
    finished_at: float | None
    task_counts: dict[str, int] = Field(default_factory=dict)
    transfer: dict[str, Any] | None = Field(None, description="disk bytes: total_bytes, done_bytes, disks, "
                                             "disks_done, disks_running (migrations and replica syncs)")


class MigrationOut(BaseModel):
    id: str
    name: str
    mode: str
    project: str
    source_endpoint_id: str
    target_endpoint_id: str
    vm: str
    options: dict[str, Any]
    phase: str | None
    disks: int | None
    crosscheck: dict[str, int] | None
    created_at: float
    updated_at: float
    last_execution: ExecutionOut | None = None


class TaskOut(BaseModel):
    id: str
    kind: str
    label: str
    seq: int
    depends_on: list[str]
    params: dict[str, Any]
    status: str
    attempts: int
    max_attempts: int
    progress: float
    message: str
    error: str | None
    result: dict[str, Any]
    started_at: float | None
    finished_at: float | None


ReplicaOut.model_rebuild()
