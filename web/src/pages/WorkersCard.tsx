// Purpose: Settings -> Workers. One row per worker host (an autoscaling supervisor, or a legacy single
// worker) with its job processes underneath, live load, and admin controls: defaults, per-host mode
// (auto: the host decides from its load; manual: the admin sets the number of workers with -/+ up to the
// threshold), pause (drain) / resume, add a host (enrollment token), remove a stale entry.
// Usage: <WorkersCard admin={bool} onToast={fn} />

import { useState } from "react";
import { Link } from "react-router-dom";
import { api, fmt } from "../api";
import { ErrorBox, Modal, Status, errText, useLoad } from "../ui";

interface Info { role?: "supervisor" | "job" | "single"; parent?: string; running?: number; queued?: number;
  decision?: string; mode?: "auto" | "manual"; manual_slots?: number; min_slots?: number; max_slots?: number; max_slots_auto?: boolean; paused?: boolean;
  cpu_count?: number; cpu_pct?: number | null; mem_free_pct?: number | null; staging_free_gib?: number | null }
export interface WorkerRow { id: string; hostname: string; pid: number; heartbeat_at: number; current_execution: string | null;
  alive: boolean; region?: string | null; capabilities?: string[] | null; info?: Info | null }
type Vals = Partial<Record<"manual_slots" | "min_slots" | "max_slots" | "cpu_max_pct" | "mem_min_free_pct" | "staging_min_free_gib" |
  "spawn_interval_s", number | null>> & { paused?: boolean | null; mode?: "auto" | "manual" | null };
interface WS { builtin: Vals; defaults: Vals; hosts: Record<string, Vals>; effective: Record<string, Vals> }

type NumKey = Exclude<keyof Vals, "paused" | "mode">;
const THRESHOLD = { k: "max_slots" as NumKey, label: "Threshold (maximum jobs)", help: "Never more jobs at once, in either mode. Empty = automatic (half the CPUs, 1–4)." };
const MANUAL: { k: NumKey; label: string; help: string; unit?: string }[] = [
  { k: "manual_slots", label: "Workers", help: "Exactly this many jobs may run at once; no load checks. Up to the threshold." },
  THRESHOLD,
];
const FIELDS: { k: NumKey; label: string; help: string; unit?: string }[] = [
  { k: "min_slots", label: "Minimum jobs", help: "Always allowed at once, whatever the load." },
  THRESHOLD,
  { k: "cpu_max_pct", label: "CPU limit", unit: "%", help: "Above the minimum, start another job only while CPU is below this." },
  { k: "mem_min_free_pct", label: "Free memory floor", unit: "%", help: "…and while at least this much RAM is free." },
  { k: "staging_min_free_gib", label: "Free staging space", unit: "GiB", help: "…and while the staging disk has this much free." },
  { k: "spawn_interval_s", label: "Start interval", unit: "s", help: "Wait between starts above the minimum, so the last job's load shows first." },
];

function SettingsModal({ host, ws, onClose, onDone }: { host: string | null; ws: WS; onClose: () => void; onDone: (m: string) => void }) {
  const stored = host ? ws.hosts[host] ?? {} : ws.defaults;
  const inherit: Vals = host ? { ...ws.builtin, ...strip(ws.defaults) } : ws.builtin;
  const all = [...MANUAL, ...FIELDS.filter((f) => f.k !== "max_slots")];
  const [v, setV] = useState<Record<string, string>>(() =>
    Object.fromEntries(all.map((f) => [f.k, stored[f.k] == null ? "" : String(stored[f.k])])));
  const [mode, setMode] = useState<string>(stored.mode ?? "");
  const shown = (mode || inherit.mode || "auto") === "manual" ? MANUAL : FIELDS;
  const [err, setErr] = useState<string | null>(null);
  const body = () => {
    const out: Record<string, number | boolean | string | null> = { paused: stored.paused ?? null, mode: mode || null };
    for (const f of all) out[f.k] = v[f.k] === "" ? null : Number(v[f.k]);
    return out;
  };
  const save = async () => {
    try {
      await api.put(host ? `/workers/settings/hosts/${encodeURIComponent(host)}` : "/workers/settings/defaults", body());
      onDone(host ? `Saved settings for ${host}` : "Saved worker defaults");
    } catch (e) { setErr(errText(e)); }
  };
  const reset = async () => {
    try { await api.del(`/workers/settings/hosts/${encodeURIComponent(host!)}`); onDone(`${host} now uses the defaults`); }
    catch (e) { setErr(errText(e)); }
  };
  return (
    <Modal title={host ? `Worker host ${host}` : "Worker defaults (all hosts)"} onClose={onClose} footer={<>
      {host && Object.keys(stored).length > 0 && <button className="btn" onClick={reset}>Use defaults</button>}
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" onClick={save}>Save</button></>}>
      <p className="small muted" style={{ marginTop: 0 }}>Each job runs in its own process on the host. Leave a field empty to
        {host ? " use the default" : " use the built-in value"} (shown greyed). Changes apply within seconds; running jobs are never stopped.</p>
      <div className="form cols">
        <label className="field wide"><span>Mode</span>
          <select value={mode} onChange={(e) => setMode(e.target.value)}>
            {host && <option value="">{`Same as the defaults (${inherit.mode ?? "auto"})`}</option>}
            {!host && <option value="">Auto (built-in)</option>}
            <option value="auto">Auto: the host starts jobs while it has spare CPU, memory and staging space</option>
            <option value="manual">Manual: you set the number of workers, up to the threshold</option></select></label>
        {shown.map((f) => (
          <label key={f.k} className="field"><span>{f.label}{f.unit ? ` (${f.unit})` : ""}</span>
            <input type="number" min={0} value={v[f.k]} placeholder={inherit[f.k] == null ? "auto" : String(inherit[f.k])}
                   onChange={(e) => setV((x) => ({ ...x, [f.k]: e.target.value }))} />
            <small>{f.help}</small></label>))}
        <div className="wide"><ErrorBox error={err} title="Not saved" /></div>
      </div>
    </Modal>);
}

function strip(v: Vals): Vals { return Object.fromEntries(Object.entries(v).filter(([, x]) => x != null)) as Vals; }

function AddHost({ onClose }: { onClose: () => void }) {
  const [tok, setTok] = useState<{ token: string; expires_at: number } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  return (
    <Modal title="Add a worker host" onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Close</button>
      {!tok && <button className="btn primary" onClick={async () => {
        try { setTok(await api.post("/workers/enroll-token", { ttl_minutes: 60 })); } catch (e) { setErr(errText(e)); }
      }}>Create join token</button>}</>}>
      <div className="form">
        <p className="small" style={{ margin: 0 }}><b>More jobs on an existing host:</b> raise its maximum (Settings on the host's row).
          No new machine needed.</p>
        <p className="small" style={{ margin: 0 }}><b>A new machine:</b> create a single-use token (valid one hour) and run this on it.
          The API must be reachable over HTTPS and use a shared Postgres database.</p>
        {tok && <pre className="mono small" style={{ whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
          {`curl -fsSL https://raw.githubusercontent.com/harshallakare/Margarita/main/scripts/deploy.sh -o deploy.sh\nsudo bash deploy.sh --enroll ${window.location.origin} --enroll-token ${tok.token}`}</pre>}
        {tok && <p className="muted small" style={{ margin: 0 }}>Expires {fmt.time(tok.expires_at)}. It is shown only once.</p>}
        <p className="small muted" style={{ margin: 0 }}>To start machines automatically when work piles up, use a worker pool with
          auto-provisioning (Worker pools).</p>
        <ErrorBox error={err} title="Could not create a token" />
      </div>
    </Modal>);
}

function load(i: Info) {
  const parts = [i.cpu_pct != null && `CPU ${Math.round(i.cpu_pct)}%`, i.mem_free_pct != null && `${Math.round(i.mem_free_pct)}% RAM free`,
    i.staging_free_gib != null && `${Math.round(i.staging_free_gib)} GiB staging free`].filter(Boolean);
  return parts.join(" · ");
}

export default function WorkersCard({ admin, onToast }: { admin: boolean; onToast: (m: string) => void }) {
  const workers = useLoad(() => api.get<WorkerRow[]>("/workers"), [], 5000);
  const ws = useLoad(() => api.get<WS>("/workers/settings"), [], 15000);
  const [edit, setEdit] = useState<string | null | undefined>(undefined);   // undefined: closed, null: defaults
  const [add, setAdd] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const rows = workers.data ?? [];
  const sups = new Set(rows.filter((w) => w.info?.role === "supervisor").map((w) => w.id));
  const top = rows.filter((w) => !(w.info?.role === "job" && w.info.parent && sups.has(w.info.parent)));
  const jobsOf = (id: string) => rows.filter((w) => w.info?.role === "job" && w.info.parent === id);
  const running = rows.filter((w) => w.alive && w.current_execution).length;
  const act = (p: Promise<unknown>, msg: string) =>
    p.then(() => { onToast(msg); workers.reload(); ws.reload(); }).catch((e) => setErr(errText(e)));
  const pause = (w: WorkerRow, paused: boolean) =>
    act(api.put(`/workers/settings/hosts/${encodeURIComponent(w.hostname)}`, { ...(ws.data?.hosts[w.hostname] ?? {}), paused }),
        paused ? `${w.hostname}: paused; running jobs finish, no new ones start` : `${w.hostname}: resumed`);
  const setWorkers = (w: WorkerRow, n: number) =>
    act(api.put(`/workers/settings/hosts/${encodeURIComponent(w.hostname)}`,
                { ...(ws.data?.hosts[w.hostname] ?? {}), mode: "manual", manual_slots: n }),
        `${w.hostname}: ${n} worker(s)`);
  const remove = (w: WorkerRow) => {
    if (!confirm(`Remove the stale entry ${w.id}? (It only clears the list; a worker that comes back re-registers.)`)) return;
    act(api.del(`/workers/${encodeURIComponent(w.id)}`), `Removed ${w.id}`);
  };
  return (
    <div className="card">
      <div className="spread" style={{ marginBottom: 12 }}><h2 style={{ margin: 0 }}>Workers</h2>
        <div className="row" style={{ gap: 8 }}>
          <span className="muted small">{top.filter((w) => w.alive).length} host(s) · {running} job(s) running</span>
          {admin && <button className="btn sm" disabled={!ws.data} onClick={() => setEdit(null)}>Defaults</button>}
          {admin && <button className="btn sm" onClick={() => setAdd(true)}>Add host</button>}</div></div>
      <ErrorBox error={err ?? workers.error} />
      {top.length === 0 ? <p className="muted">No workers have checked in. Start one with <code>vmigrate-platform worker</code>.</p> : (
        <div className="table-wrap"><table>
          <thead><tr><th>Worker</th><th>Jobs</th><th>State</th><th>Heartbeat</th>{admin && <th />}</tr></thead>
          <tbody>{top.map((w) => {
            const i = w.info ?? {};
            const sup = i.role === "supervisor";
            const jobs = sup ? jobsOf(w.id) : [];
            const hs = ws.data?.hosts[w.hostname] ?? {};
            const slots = hs.manual_slots ?? i.manual_slots ?? 0;          // saved value: the heartbeat lags a save
            const thr = hs.max_slots ?? i.max_slots ?? 0;
            const custom = sup && !!ws.data?.hosts[w.hostname] && Object.keys(ws.data.hosts[w.hostname]).some((k) => k !== "paused");
            return [
              <tr key={w.id}>
                <td><span className="mono small">{w.id}</span>
                  <div className="muted small">{sup ? (i.mode === "manual" ? "manual" : "autoscaling") : i.role === "job" ? "job process" : "single worker"}
                    {w.region ? ` · region ${w.region}` : ""}{sup && i.cpu_count ? ` · ${i.cpu_count} CPUs` : ""}
                    {custom && " · custom limits"}</div>
                  {sup && w.alive && load(i) && <div className="muted small">{load(i)}</div>}
                  <div className="muted small" title={(w.capabilities ?? []).join("\n")}>
                    {(w.capabilities ?? []).filter((c) => !c.startsWith("provider:")).join(" · ") || "—"}</div></td>
                <td className="small">{sup ? <>
                    {i.mode === "manual" ? <div className="row" style={{ gap: 6, alignItems: "center" }}>
                      <span><b>{i.running ?? 0}</b> busy of</span>
                      {admin && <button className="btn sm" disabled={!w.alive || slots <= 0}
                        title="Remove a worker (a running job is never stopped)" onClick={() => setWorkers(w, slots - 1)}>−</button>}
                      <b>{slots}</b>
                      {admin && <button className="btn sm" disabled={!w.alive || slots >= thr}
                        title={slots >= thr ? `At the threshold of ${thr}; raise it in Settings` : "Add a worker"}
                        onClick={() => setWorkers(w, slots + 1)}>+</button>}
                      <span className="muted">worker(s)</span></div>
                    : <><b>{i.running ?? 0}</b> / {i.max_slots ?? "?"}{i.max_slots_auto ? " (auto)" : ""}</>}
                    <div className="muted">{i.mode === "manual" ? `manual · threshold ${thr}` : `auto · min ${i.min_slots ?? 0}`}
                      {i.queued ? ` · ${i.queued} queued` : ""}</div>
                    {w.alive && i.decision && <div className="muted" title="why the next job does or does not start">{i.decision}</div>}</>
                  : w.current_execution ? <Link to={`/executions/${w.current_execution}`}>1 running</Link> : "0"}</td>
                <td>{!w.alive ? <Status s="failed" label="stale" /> : i.paused ? <Status s="pending" label="paused" />
                  : (sup ? (i.running ?? 0) > 0 : !!w.current_execution) ? <Status s="running" label="busy" /> : <Status s="ok" label="idle" />}</td>
                <td className="small muted">{fmt.ago(w.heartbeat_at)}</td>
                {admin && <td><div className="row" style={{ justifyContent: "end", gap: 6 }}>
                  {!w.alive ? <button className="btn sm danger" onClick={() => remove(w)}>Remove</button> : sup && <>
                    <button className="btn sm" disabled={!ws.data} onClick={() => setEdit(w.hostname)}>Settings</button>
                    {i.paused ? <button className="btn sm" onClick={() => pause(w, false)}>Resume</button>
                      : <button className="btn sm" onClick={() => pause(w, true)} title="Start no new jobs on this host; running ones finish">Pause</button>}</>}
                </div></td>}
              </tr>,
              ...jobs.map((j) => (
                <tr key={j.id} className="sub">
                  <td style={{ paddingLeft: 24 }}><span className="mono small muted">↳ {j.id}</span></td>
                  <td className="small">{j.current_execution ? <Link to={`/executions/${j.current_execution}`}>open job</Link> : <span className="muted">starting</span>}</td>
                  <td>{j.alive ? <Status s="running" label="running" /> : <Status s="failed" label="stale" />}</td>
                  <td className="small muted">{fmt.ago(j.heartbeat_at)}</td>{admin && <td />}</tr>)),
            ];
          })}</tbody>
        </table></div>)}
      <p className="muted small" style={{ marginBottom: 0 }}>Each host runs one process per job. <b>Auto</b>: up to the threshold, and above
        the minimum only while CPU, memory and staging space allow. <b>Manual</b>: exactly the number of workers you set (−/+), up to the
        threshold, without load checks. Pause drains a host before maintenance.</p>
      {edit !== undefined && ws.data && <SettingsModal host={edit} ws={ws.data} onClose={() => setEdit(undefined)}
        onDone={(m) => { setEdit(undefined); onToast(m); ws.reload(); workers.reload(); }} />}
      {add && <AddHost onClose={() => setAdd(false)} />}
    </div>);
}
