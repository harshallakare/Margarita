// Purpose: typed client for the vmigrate REST API + the SSE event stream. The bearer token lives in
// sessionStorage (cleared when the tab closes); credentials typed into forms are sent once and never
// read back (the API does not return them).
// Usage: import { api, type Migration } from "./api"; await api.get<Migration[]>("/migrations");

export type Role = "viewer" | "operator" | "admin";
export interface Me { user_id: string; username: string; role: Role; project: string }

export interface Field {
  name: string; label: string; kind: "text" | "url" | "password" | "bool" | "int" | "json" | "textarea" | "select";
  options?: string[];
  secret?: boolean; required?: boolean; help?: string; default?: unknown;
}
export interface ProviderRow { type: string; status: string; notes: string; fields: Field[] }
export interface Providers { sources: ProviderRow[]; targets: ProviderRow[] }

export interface Endpoint {
  id: string; name: string; type: string; project: string; description: string;
  config: Record<string, unknown>; has_credentials: boolean; credential_keys: string[];
  region?: string | null; required_tags?: string[];
  created_at: number; updated_at: number;
}

export interface Execution {
  id: string; migration_id: string; kind: ExecKind; dry_run: boolean; status: ExecStatus;
  params?: Record<string, unknown> | null;
  region?: string | null; waiting_reason?: string | null;
  error: string | null; cancel_requested: boolean; lease_owner: string | null; runs: number;
  created_at: number; started_at: number | null; finished_at: number | null;
  task_counts: Record<string, number>;
  transfer?: Transfer | null;
}
/** Disk bytes moved by an execution's copy tasks (migrations, replica syncs, cutovers). */
export interface Transfer { total_bytes: number; done_bytes: number; disks: number; disks_done: number; disks_running: number }
export type ExecKind = "migration" | "replica_sync" | "replica_deploy" | "replica_delete" | "replica_cutover";
export const KIND_LABEL: Record<string, string> = {
  migration: "Migration run", replica_sync: "Replica sync", replica_deploy: "Deploy (failover)",
  replica_delete: "Delete replica disks", replica_cutover: "Cutover",
};
export type ExecStatus = "pending" | "queued" | "running" | "completed" | "failed" | "canceled";

export interface Migration {
  id: string; name: string; mode: string; project: string; source_endpoint_id: string;
  target_endpoint_id: string; vm: string; options: MigrationOptions; phase: string | null;
  disks: number | null; crosscheck: Record<string, number> | null; created_at: number; updated_at: number;
  last_execution: Execution | null;
}
export interface MigrationOptions {
  target_name?: string; app_consistent?: boolean; guest_os?: Record<string, unknown>;
  target_options?: Record<string, unknown>; osmorph?: Record<string, unknown>; engine?: Record<string, unknown>;
  retention?: number;
}

export interface Schedule {
  id: string; replica_id: string; cron: string; enabled: boolean; next_run_at: number | null;
  last_run_at: number | null; last_execution_id: string | null; last_skip_reason: string | null; upcoming: number[];
}
export interface SyncRecord {
  id: string; started: number; finished: number; ok: boolean; snapshot_id: string; modes: string[];
  restore_point: string | null; bytes_read: number; bytes_written: number; bytes_unchanged: number;
}
export interface Replica {
  id: string; name: string; project: string; source_endpoint_id: string; target_endpoint_id: string; vm: string;
  options: MigrationOptions; disks: number | null; disk_total_bytes: number | null;
  crosscheck: Record<string, number> | null; consistent: boolean;
  current_sync: { id: string; started: number; last_error: string | null; disks_done: number } | null;
  last_sync: SyncRecord | null; syncs: SyncRecord[];
  restore_points: { id: string; at: number; disks: Record<string, string> }[];
  deployments: { id: string; name: string; at: number; status: string; restore_point: string | null;
                 target_vm?: { id: string; name: string } | null }[];
  errors: { t: number; kind: string; error: string }[];
  schedules: Schedule[]; created_at: number; updated_at: number; last_execution: Execution | null;
}

export interface Task {
  id: string; kind: string; label: string; seq: number; depends_on: string[]; params: Record<string, unknown>;
  status: "pending" | "running" | "completed" | "failed" | "canceled"; attempts: number; max_attempts: number;
  progress: number; message: string; error: string | null; result: Record<string, unknown>;
  started_at: number | null; finished_at: number | null;
}
export interface LogLine { id: number; task_id: string | null; ts: number; level: string; message: string }

export interface DiskIR {
  id: string; bus: string; size_bytes: number; order: number; is_boot: boolean; bus_index?: number;
}
export interface VmIR {
  name: string; source_id: string; cpu: { sockets: number; cores: number }; memory_bytes: number;
  firmware: string; guest_os: { family: string; distro?: string; version?: string };
  disks: DiskIR[]; nics: { id: string; network: string; mac?: string }[];
}
export interface Inspect {
  vm: VmIR;
  disk_gate: { ok: boolean; counts: Record<string, number>; error?: string; legacy_cap_exceeded: boolean };
}
export interface Report {
  migration: string; phase: string; crosscheck: Record<string, number> | null; disk_count_match: boolean;
  disks: { id: string; order: number; bus: string; size_bytes: number; copied: boolean; committed: boolean;
           attached: boolean; verify: Record<string, unknown> | null }[];
  morph: Record<string, unknown> | null; verification: Record<string, unknown> | null;
  errors: { phase: string; error: string }[]; target_vm: Record<string, unknown> | null;
}

const KEY = "vmigrate.token";
export const token = {
  get: () => { try { return sessionStorage.getItem(KEY); } catch { return null; } },
  set: (t: string | null) => {
    try { if (t) sessionStorage.setItem(KEY, t); else sessionStorage.removeItem(KEY); } catch { /* private mode */ }
  },
};

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

let onUnauthorized: () => void = () => {};
export const setUnauthorizedHandler = (fn: () => void) => { onUnauthorized = fn; };

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  const t = token.get();
  if (t) headers.Authorization = `Bearer ${t}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const r = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (r.status === 401 && path !== "/auth/token") { token.set(null); onUnauthorized(); }
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const j = await r.json();
      msg = typeof j.detail === "string" ? j.detail
        : Array.isArray(j.detail) ? j.detail.map((d: { loc?: string[]; msg: string }) =>
            `${(d.loc ?? []).slice(1).join(".")}: ${d.msg}`).join("; ")
        : j.detail && typeof j.detail.message === "string"      // {message, problems[]} (e.g. endpoint validation)
          ? [j.detail.message, ...((j.detail.problems ?? []) as string[]).map((p) => `• ${p}`)].join("\n")
        : JSON.stringify(j);
    } catch { /* not json */ }
    throw new ApiError(r.status, msg);
  }
  if (r.status === 204) return undefined as T;
  const ct = r.headers.get("content-type") ?? "";
  return (ct.includes("json") ? r.json() : r.text()) as Promise<T>;
}

/** Authenticated file download (diagnostics zip, CSV export, reports). */
export async function download(path: string, filename: string): Promise<void> {
  const t = token.get();
  const r = await fetch(path, { headers: { Authorization: `Bearer ${t ?? ""}` } });
  if (!r.ok) throw new Error(`download failed: HTTP ${r.status}`);
  const url = URL.createObjectURL(await r.blob());
  const a = Object.assign(document.createElement("a"), { href: url, download: filename });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export const api = {
  get: <T,>(p: string) => req<T>("GET", p),
  post: <T,>(p: string, b?: unknown) => req<T>("POST", p, b ?? {}),
  patch: <T,>(p: string, b: unknown) => req<T>("PATCH", p, b),
  del: (p: string) => req<void>("DELETE", p),
};

export interface StreamEvent { id: number; type: string; data: Record<string, unknown> }

/** Subscribe to /events (SSE). EventSource cannot send headers, so the token goes in the query. */
export function subscribe(filter: { execution_id?: string; migration_id?: string },
                          onEvent: (e: StreamEvent) => void): () => void {
  const q = new URLSearchParams({ ...filter, access_token: token.get() ?? "" } as Record<string, string>);
  const es = new EventSource(`/events?${q}`);
  const types = ["execution.created", "execution.queued", "execution.running", "execution.completed",
    "execution.failed", "execution.canceled", "task.started", "task.progress", "task.completed",
    "task.failed", "task.canceled", "task.retry"];
  const h = (ev: MessageEvent) => {
    try { onEvent({ id: Number(ev.lastEventId), type: ev.type, data: JSON.parse(ev.data) }); } catch { /* skip */ }
  };
  types.forEach((t) => es.addEventListener(t, h as EventListener));
  return () => es.close();
}

export const fmt = {
  bytes(n: number | null | undefined): string {
    if (n == null) return "—";
    const u = ["B", "KiB", "MiB", "GiB", "TiB"];
    let i = 0; let v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(i && v < 10 ? 1 : 0)} ${u[i]}`;
  },
  time(t: number | null | undefined): string {
    return t ? new Date(t * 1000).toLocaleString() : "—";
  },
  ago(t: number | null | undefined): string {
    if (!t) return "—";
    const s = Math.max(0, Date.now() / 1000 - t);
    if (s < 60) return `${Math.floor(s)}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  },
  secs(s: number): string {
    if (!isFinite(s) || s < 0) return "—";
    if (s < 60) return `${Math.ceil(s)}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${Math.floor(s % 60)}s`;
    return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  },
  dur(a: number | null | undefined, b: number | null | undefined): string {
    if (!a) return "—";
    const s = Math.max(0, (b ?? Date.now() / 1000) - a);
    if (s < 60) return `${s.toFixed(0)}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${Math.floor(s % 60)}s`;
    return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  },
};
