// Purpose: investigation — search run logs across every execution (level, text, migration, time range)
// and browse platform events (run lifecycle, workers, schedules, requeues). Each row links to its run;
// failures carry the full traceback (click a row to expand it).

import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, fmt, KIND_LABEL, type Migration } from "../api";
import { ErrorBox, Loading, useLoad } from "../ui";

interface LogRow { id: number; execution_id: string; task_id: string | null; ts: number; level: string; message: string;
  logger?: string | null; kind?: string; migration_id?: string; migration_name?: string | null }
interface EventRow { id: number; execution_id: string | null; migration_id: string | null; ts: number; type: string;
  payload: Record<string, unknown> }

const LEVELS = ["ERROR", "WARNING", "INFO", "DEBUG"];
const RANGES: [string, number][] = [["last hour", 3600], ["last 24 h", 86400], ["last 7 days", 7 * 86400], ["all time", 0]];

function since(sec: number): string { return sec ? `&since=${Date.now() / 1000 - sec}` : ""; }

export default function Logs() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "events" ? "events" : "logs";
  const [q, setQ] = useState(params.get("q") ?? "");
  const [levels, setLevels] = useState<string[]>(params.get("level")?.split(",") ?? ["ERROR", "WARNING"]);
  const [mig, setMig] = useState(params.get("migration_id") ?? "");
  const [range, setRange] = useState(86400);
  const [typ, setTyp] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  // deleted migrations / replicas keep their runs and logs, so they stay selectable here
  const migs = useLoad(() => Promise.all([api.get<Migration[]>("/migrations"), api.get<Migration[]>("/replicas"),
    api.get<Migration[]>("/migrations?deleted=true")]).then(([a, b, c]) => [...a, ...b, ...c]));
  const live = (migs.data ?? []).filter((m) => !m.deleted_at);
  const gone = (migs.data ?? []).filter((m) => m.deleted_at);
  const query = useMemo(() => {
    const p = new URLSearchParams();
    if (q.trim()) p.set("q", q.trim());
    if (levels.length) p.set("level", levels.join(","));
    if (mig) p.set("migration_id", mig);
    return p.toString();
  }, [q, levels, mig]);
  const logs = useLoad(() => tab === "logs" ? api.get<LogRow[]>(`/logs?${query}${since(range)}&limit=500`) : Promise.resolve([]),
                       [tab, query, range], tab === "logs" ? 10000 : 0);
  const events = useLoad(() => tab === "events"
    ? api.get<EventRow[]>(`/events/history?limit=500${mig ? `&migration_id=${mig}` : ""}${typ ? `&type=${typ}` : ""}${since(range)}`)
    : Promise.resolve([]), [tab, mig, typ, range], tab === "events" ? 10000 : 0);
  const name = (id: string | null | undefined) => (migs.data ?? []).find((m) => m.id === id)?.name ?? (id ? id.slice(0, 8) : "platform");
  const toggle = (l: string) => setLevels(levels.includes(l) ? levels.filter((x) => x !== l) : [...levels, l]);

  return (
    <>
      <div className="page-head"><div><h1>Logs</h1>
        <div className="sub">Find what went wrong: every run's log lines, with full tracebacks for failures, and the platform's event history.
          For one run, its page has a <b>Download diagnostics</b> button with everything in one file.</div></div></div>
      <div className="tabs" role="tablist" style={{ marginBottom: 12 }}>
        <button className={`tab ${tab === "logs" ? "on" : ""}`} onClick={() => setParams({ tab: "logs" })}>Run logs</button>
        <button className={`tab ${tab === "events" ? "on" : ""}`} onClick={() => setParams({ tab: "events" })}>Platform events</button>
      </div>
      <div className="card" style={{ marginBottom: 12 }}>
        <div className="row" style={{ gap: 12, flexWrap: "wrap", alignItems: "end" }}>
          {tab === "logs" && <label className="field" style={{ flex: "2 1 240px" }}><span>Search text</span>
            <input type="text" value={q} placeholder="e.g. timeout, HTTP 401, disk scsi.3" onChange={(e) => setQ(e.target.value)} /></label>}
          {tab === "events" && <label className="field" style={{ flex: "1 1 200px" }}><span>Event type</span>
            <select value={typ} onChange={(e) => setTyp(e.target.value)}>
              <option value="">all</option><option value="execution.">runs</option><option value="task.failed">failed tasks</option>
              <option value="worker.">workers</option><option value="pool.">pools</option></select></label>}
          <label className="field" style={{ flex: "1 1 200px" }}><span>Migration / replica</span>
            <select value={mig} onChange={(e) => setMig(e.target.value)}>
              <option value="">all</option>
              {live.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
              {gone.length > 0 && <optgroup label="Deleted (logs kept)">
                {gone.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}</optgroup>}</select></label>
          <label className="field" style={{ flex: "1 1 140px" }}><span>Time</span>
            <select value={range} onChange={(e) => setRange(Number(e.target.value))}>
              {RANGES.map(([l, s]) => <option key={l} value={s}>{l}</option>)}</select></label>
          {tab === "logs" && <div className="field"><span>Level</span><div className="row" style={{ gap: 6 }}>
            {LEVELS.map((l) => <button key={l} className={`btn sm ${levels.includes(l) ? "primary" : ""}`} onClick={() => toggle(l)}>{l}</button>)}
          </div></div>}
        </div>
      </div>

      {tab === "logs" ? (
        <div className="card">
          <ErrorBox error={logs.error} />
          {logs.loading && !logs.data ? <Loading /> : (logs.data ?? []).length === 0 ? <p className="muted">No log lines match.</p> : (
            <div className="table-wrap"><table className="logtable">
              <thead><tr><th style={{ width: 150 }}>Time</th><th style={{ width: 80 }}>Level</th><th>Message</th><th style={{ width: 200 }}>Run</th></tr></thead>
              <tbody>{logs.data!.map((r) => {
                const lines = r.message.split("\n");
                const long = lines.length > 1;
                return (
                  <tr key={r.id} className={long ? "click" : ""} onClick={() => long && setOpen(open === r.id ? null : r.id)}>
                    <td className="small mono">{new Date(r.ts * 1000).toLocaleString()}</td>
                    <td><span className={`lvl lvl-${r.level}`}>{r.level}</span></td>
                    <td className="small"><div className="mono" style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                      {open === r.id ? r.message : lines[0]}{long && open !== r.id && <span className="muted"> … (traceback, click)</span>}</div>
                      <div className="muted">{r.task_id ?? ""}{r.logger ? ` · ${r.logger}` : ""}</div></td>
                    <td className="small"><Link to={`/executions/${r.execution_id}`} onClick={(e) => e.stopPropagation()}>
                      {r.migration_name ?? r.execution_id.slice(0, 8)}</Link>
                      <div className="muted">{KIND_LABEL[r.kind ?? ""] ?? r.kind}</div></td>
                  </tr>);
              })}</tbody>
            </table></div>)}
        </div>
      ) : (
        <div className="card">
          <ErrorBox error={events.error} />
          {events.loading && !events.data ? <Loading /> : (events.data ?? []).length === 0 ? <p className="muted">No events.</p> : (
            <div className="table-wrap"><table>
              <thead><tr><th style={{ width: 150 }}>Time</th><th style={{ width: 170 }}>Event</th><th>Details</th><th style={{ width: 180 }}>Where</th></tr></thead>
              <tbody>{events.data!.map((e) => (
                <tr key={e.id}>
                  <td className="small mono">{new Date(e.ts * 1000).toLocaleString()}</td>
                  <td className="small"><b>{e.type}</b></td>
                  <td className="small mono" style={{ wordBreak: "break-word" }}>{Object.entries(e.payload ?? {})
                    .filter(([, v]) => v !== null && v !== "" && typeof v !== "object").map(([k, v]) => `${k}=${String(v)}`).join("  ").slice(0, 300)}</td>
                  <td className="small">{e.execution_id ? <Link to={`/executions/${e.execution_id}`}>{name(e.migration_id)}</Link>
                    : <span className="muted">{name(e.migration_id)}</span>}<div className="muted">{fmt.ago(e.ts)}</div></td>
                </tr>))}</tbody>
            </table></div>)}
        </div>
      )}
    </>
  );
}
