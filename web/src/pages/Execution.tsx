// Purpose: live execution view — overall progress, the task graph (per-disk copy tasks grouped under
// "Create target disks", each with its own bar, MB/s and GiB done), the selected task's details,
// streamed logs, the dry-run plan, and cancel / retry. Live updates come from SSE (/events);
// a 5 s poll is the fallback.

import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, download, fmt, KIND_LABEL, subscribe, type Execution as Exec, type LogLine, type Migration, type Task } from "../api";
import { Bar, ErrorBox, Loading, Status, Toast, TransferBar, errText, useAuth, useLoad } from "../ui";

const LIVE = ["pending", "queued", "running"];

function TaskRow({ t, sel, onClick, child }: { t: Task; sel: boolean; onClick: () => void; child?: boolean }) {
  return (
    <div className={`task ${sel ? "sel" : ""} ${child ? "child" : ""}`} onClick={onClick} role="button" tabIndex={0}
         onKeyDown={(e) => { if (e.key === "Enter") onClick(); }}>
      <span className={`dot ${t.status}`}>{t.status === "completed" ? "✓" : t.status === "failed" ? "!" : ""}</span>
      <div style={{ minWidth: 0 }}>
        <div className="lbl">{t.label || t.id}{t.attempts > 1 && <span className="muted small"> · attempt {t.attempts}/{t.max_attempts}</span>}</div>
        {t.error ? <div className="msg err">{t.error}</div> : t.message ? <div className="msg">{t.message}</div> : null}
      </div>
      <div>{(t.status === "running" || (child && t.status !== "pending")) &&
        <Bar pct={t.progress} tone={t.status === "completed" ? "ok" : t.status === "failed" ? "bad" : undefined} />}
        {child && typeof t.params.size_bytes === "number" && <div className="small muted" style={{ marginTop: 2, whiteSpace: "nowrap" }}>
          {fmt.bytes(t.status === "completed" ? t.params.size_bytes : (t.params.size_bytes * t.progress) / 100)} / {fmt.bytes(t.params.size_bytes)}</div>}</div>
      <div className="when">{t.status === "running" ? fmt.dur(t.started_at, null) : t.finished_at ? fmt.dur(t.started_at, t.finished_at) : ""}</div>
    </div>
  );
}

export default function Execution() {
  const { id } = useParams();
  const { can } = useAuth();
  const ex = useLoad(() => api.get<Exec>(`/executions/${id}`), [id], 5000);
  const tasks = useLoad(() => api.get<Task[]>(`/executions/${id}/tasks`), [id], 5000);
  const mig = useLoad(() => ex.data ? api.get<Migration>(`/migrations/${ex.data.migration_id}`) : Promise.resolve(null),
                      [ex.data?.migration_id]);
  const [sel, setSel] = useState<string | null>(null);
  const [logs, setLogs] = useState<LogLine[]>([]);
  const [onlySel, setOnlySel] = useState(false);
  const [follow, setFollow] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const logRef = useRef<HTMLDivElement>(null);
  const cursor = useRef(0);
  const live = ex.data ? LIVE.includes(ex.data.status) : true;

  // SSE: patch task progress in place; refetch structure on lifecycle events (debounced)
  useEffect(() => {
    if (!id) return;
    let t: ReturnType<typeof setTimeout> | null = null;
    const refetch = () => { if (t) clearTimeout(t); t = setTimeout(() => { tasks.reload(); ex.reload(); }, 300); };
    const off = subscribe({ execution_id: id }, (e) => {
      const d = e.data as { task_id?: string; progress?: number; message?: string; status?: Task["status"] };
      if (e.type === "task.progress" && d.task_id) {
        tasks.setData((cur) => cur?.map((x) => x.id === d.task_id
          ? { ...x, progress: d.progress ?? x.progress, message: d.message ?? x.message, status: "running" } : x) ?? cur);
      } else refetch();
    });
    return () => { off(); if (t) clearTimeout(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  // logs: incremental fetch while live, one final fetch after
  useEffect(() => {
    if (!id) return;
    cursor.current = 0; setLogs([]);
    let stop = false;
    const pull = async () => {
      try {
        const rows = await api.get<LogLine[]>(`/executions/${id}/logs?after=${cursor.current}&limit=2000`);
        if (rows.length && !stop) {
          cursor.current = rows[rows.length - 1].id;
          setLogs((l) => [...l, ...rows].slice(-5000));
        }
      } catch { /* transient */ }
    };
    pull();
    const t = setInterval(pull, 2000);
    return () => { stop = true; clearInterval(t); };
  }, [id]);

  useEffect(() => {
    if (follow && logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [logs, follow, onlySel, sel]);

  const all = tasks.data ?? [];
  const disks = all.filter((t) => t.kind === "replicate_disk");
  const top = all.filter((t) => t.kind !== "replicate_disk");
  const stepPct = all.length ? all.reduce((a, t) => a + (t.status === "completed" ? 100 : t.progress), 0) / all.length : 0;
  // when disks are copied, data dominates: 90% data moved, 10% the other steps (validate, snapshot, VM, verify)
  const tr = ex.data?.transfer;
  const overall = tr && tr.total_bytes > 0 && !ex.data?.dry_run
    ? 0.9 * (tr.done_bytes * 100 / tr.total_bytes)
      + 0.1 * (top.length ? top.reduce((a, t) => a + (t.status === "completed" ? 100 : t.progress), 0) / top.length : 0)
    : stepPct;
  const diskPct = disks.length ? disks.reduce((a, t) => a + t.progress, 0) / disks.length : 0;
  const selected = all.find((t) => t.id === sel) ?? null;
  const plan = (all.find((t) => t.kind.startsWith("plan"))?.result?.plan ?? null) as Record<string, unknown>[] | null;
  const shown = useMemo(() => (onlySel && sel ? logs.filter((l) => l.task_id === sel) : logs), [logs, onlySel, sel]);

  if (ex.loading && !ex.data) return <Loading />;
  if (!ex.data) return <ErrorBox error={ex.error} title="Execution not found" />;
  const x = ex.data;
  const failedTask = all.find((t) => t.status === "failed");
  const isRep = x.kind !== "migration";
  const home = `${isRep ? "/replicas" : "/migrations"}/${x.migration_id}`;
  const doneMsg: Record<string, string> = {
    migration: `All ${disks.length} disks were copied, verified and attached.`,
    replica_sync: `All ${disks.length} replica disks are up to date and verified.`,
    replica_deploy: "The VM was deployed from the replica. The replica keeps syncing.",
    replica_delete: "Replica disks and restore points were deleted.",
    replica_cutover: "Cutover finished: the VM now runs on the target from the final sync.",
  };

  return (
    <>
      <div className="page-head">
        <div>
          <div className="crumbs"><Link to={isRep ? "/replicas" : "/migrations"}>{isRep ? "Replicas" : "Migrations"}</Link> / {mig.data ? <Link to={home}>{mig.data.name}</Link> : "…"} /</div>
          <div className="row"><h1>{KIND_LABEL[x.kind] ?? x.kind}{x.dry_run ? " · dry-run" : ""}</h1><Status s={x.status} />
            {x.dry_run && <span className="tag">nothing is changed</span>}
            {x.cancel_requested && LIVE.includes(x.status) && <span className="pill warn">cancelling</span>}</div>
          <div className="sub mono">{x.id}{x.lease_owner ? ` · worker ${x.lease_owner}` : ""}{x.runs > 1 ? ` · attempt ${x.runs}` : ""}</div>
        </div>
        <div className="row">
          <button className="btn" title="Tasks, logs with tracebacks, events, state, report, redacted endpoints, versions"
                  onClick={() => download(`/executions/${x.id}/diagnostics`, `margarita-diagnostics-${x.id.slice(0, 8)}.zip`)
                    .catch((e) => setErr(errText(e)))}>Download diagnostics</button>
          {can("admin") && <Link className="btn ghost" to={`/audit?object_id=${x.migration_id}`}>Audit trail</Link>}
        </div>
        {can("operator") && <div className="row">
          {live && !x.cancel_requested && <button className="btn danger" onClick={async () => {
            try { await api.post(`/executions/${x.id}/cancel`); setToast("Cancel requested; running disk copies stop at their next checkpoint"); ex.reload(); }
            catch (e) { setErr(errText(e)); }
          }}>Cancel</button>}
          {(x.status === "failed" || x.status === "canceled") && <button className="btn primary" onClick={async () => {
            try { await api.post(`/executions/${x.id}/retry`); setToast("Re-queued: completed tasks are kept"); ex.reload(); tasks.reload(); }
            catch (e) { setErr(errText(e)); }
          }}>Retry (resume)</button>}
        </div>}
      </div>
      <ErrorBox error={err} title="Action failed" />
      {x.status === "queued" && x.waiting_reason && <div className="alert warn" style={{ marginBottom: 16 }}>
        <b>Waiting for a suitable worker.</b> {x.waiting_reason}</div>}
      {x.status === "failed" && <div className="alert bad" style={{ marginBottom: 16 }}><b>Failed{failedTask ? ` in “${failedTask.label}”` : ""}.</b>
        <pre>{x.error}</pre></div>}
      {x.status === "completed" && !x.dry_run && <div className="alert ok" style={{ marginBottom: 16 }}>
        <b>Done.</b> {doneMsg[x.kind]} Back to <Link to={home}>{mig.data?.name ?? "overview"}</Link>.</div>}
      {x.status === "completed" && x.dry_run && <div className="alert info" style={{ marginBottom: 16 }}>
        <b>Dry-run passed.</b> Review the plan below, then start the real run from <Link to={home}>{mig.data?.name ?? "the overview page"}</Link>.</div>}

      <div className="grid cols-4">
        <div className="card stat"><div className="k">Overall</div><div className="v">{overall.toFixed(0)}<small>%</small></div>
          <Bar pct={overall} lg tone={x.status === "completed" ? "ok" : x.status === "failed" ? "bad" : undefined} /></div>
        <div className="card stat"><div className="k">Tasks</div><div className="v">{x.task_counts.completed ?? 0}<small> / {all.length}</small></div></div>
        <div className="card stat"><div className="k">Disks copied</div><div className="v">
          {disks.length ? <>{disks.filter((d) => d.status === "completed").length}<small> / {disks.length} · {diskPct.toFixed(0)}%</small></> : "—"}</div></div>
        <div className="card stat"><div className="k">Elapsed</div><div className="v" style={{ fontSize: 18 }}>{fmt.dur(x.started_at, x.finished_at)}</div>
          <div className="small muted">queued {fmt.ago(x.created_at)}</div></div>
      </div>

      {!x.dry_run && x.transfer && x.transfer.total_bytes > 0 && <div className="card" style={{ marginTop: 16 }}>
        <h2>Data copied</h2>
        <TransferBar id={x.id} x={x.transfer} status={x.status} />
      </div>}

      <div className="split">
        <div className="card">
          <h2>Tasks</h2>
          {tasks.loading && !tasks.data ? <Loading /> : (
            <div className="tasks">{top.map((t) => (
              <div key={t.id}>
                <TaskRow t={t} sel={sel === t.id} onClick={() => setSel(sel === t.id ? null : t.id)} />
                {(t.kind === "deploy_disks" || t.kind === "deploy_replica_disks") && disks.map((d) =>
                  <TaskRow key={d.id} t={d} child sel={sel === d.id} onClick={() => setSel(sel === d.id ? null : d.id)} />)}
              </div>))}</div>)}
        </div>
        <div className="stack" style={{ marginTop: 0 }}>
          <div className="card">
            <div className="spread" style={{ marginBottom: 8 }}>
              <h2 style={{ margin: 0 }}>Logs</h2>
              <div className="row small">
                <label className="check"><input type="checkbox" checked={onlySel} disabled={!sel} onChange={(e) => setOnlySel(e.target.checked)} />selected task only</label>
                <label className="check"><input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />follow</label>
              </div>
            </div>
            <div className="logs" ref={logRef} aria-live="polite">
              {shown.length === 0 ? <span className="t">{live ? "waiting for output…" : "no log lines"}</span> : shown.map((l) => (
                <div key={l.id} className={`l-${l.level}`}>
                  <span className="t">{new Date(l.ts * 1000).toLocaleTimeString()} </span>
                  {l.task_id && <span className="tid">[{l.task_id.replace("replicate_disk:", "disk:")}] </span>}
                  {l.level !== "INFO" && <b>{l.level} </b>}{l.message}
                </div>))}
            </div>
          </div>
          {selected && (
            <div className="card">
              <div className="spread"><h2 style={{ margin: 0 }}>{selected.label}</h2><Status s={selected.status} /></div>
              <dl className="kv small" style={{ marginTop: 12 }}>
                <dt>Task</dt><dd className="mono">{selected.id}</dd>
                <dt>Depends on</dt><dd className="mono">{selected.depends_on.join(", ") || "—"}</dd>
                <dt>Attempts</dt><dd>{selected.attempts} of {selected.max_attempts}</dd>
                <dt>Started</dt><dd>{fmt.time(selected.started_at)}</dd>
                <dt>Finished</dt><dd>{fmt.time(selected.finished_at)}</dd>
              </dl>
              {selected.error && <ErrorBox error={selected.error} title="Error" />}
              {Object.keys(selected.result).length > 0 && !selected.kind.startsWith("plan") &&
                <pre className="md" style={{ marginTop: 12 }}>{JSON.stringify(selected.result, null, 2)}</pre>}
            </div>
          )}
        </div>
      </div>

      {plan && (
        <div className="card" style={{ marginTop: 16 }}>
          <h2>Dry-run plan ({plan.length} actions; nothing was changed)</h2>
          <div className="table-wrap"><table>
            <thead><tr><th>#</th><th>Action</th><th>Details</th></tr></thead>
            <tbody>{plan.map((a, i) => {
              const { action, ...rest } = a;
              return <tr key={i}><td className="mono">{i + 1}</td><td className="mono">{String(action)}</td>
                <td className="small mono" style={{ overflowWrap: "anywhere" }}>{Object.entries(rest).map(([k, v]) =>
                  `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`).join("  ")}</td></tr>;
            })}</tbody>
          </table></div>
        </div>
      )}
      <Toast msg={toast} onDone={() => setToast(null)} />
    </>
  );
}
