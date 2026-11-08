// Purpose: one migration — summary, disk-count cross-check, run controls (dry-run, real run behind a
// typed confirmation, cancel, delete), execution history, and the per-VM verification report
// (disk count match, per-disk copy/commit/attach/verify, OSMorph, boot check) with downloads.

import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, fmt, token, type Endpoint, type Execution, type Migration, type Report } from "../api";
import { ConfirmExecute, ErrorBox, Loading, Modal, Status, Toast, TransferBar, errText, useAuth, useLoad } from "../ui";

const ACTIVE = ["pending", "queued", "running"];

function Check({ ok, label }: { ok: boolean; label?: string }) {
  return ok ? <span style={{ color: "var(--ok)" }}>✓{label ? ` ${label}` : ""}</span>
    : <span className="muted">–{label ? ` ${label}` : ""}</span>;
}

async function download(path: string, filename: string) {
  const r = await fetch(path, { headers: { Authorization: `Bearer ${token.get() ?? ""}` } });
  const url = URL.createObjectURL(await r.blob());
  const a = Object.assign(document.createElement("a"), { href: url, download: filename });
  a.click();
  URL.revokeObjectURL(url);
}

export default function MigrationDetail() {
  const { id } = useParams();
  const nav = useNavigate();
  const { can } = useAuth();
  const mig = useLoad(() => api.get<Migration>(`/migrations/${id}`), [id], 3000);
  const execs = useLoad(() => api.get<Execution[]>(`/executions?migration_id=${id}`), [id], 3000);
  const eps = useLoad(() => api.get<Endpoint[]>("/endpoints"));
  const rep = useLoad(() => api.get<Report>(`/migrations/${id}/report`).catch(() => null), [id, mig.data?.phase], 0);
  const [confirm, setConfirm] = useState(false);
  const [del, setDel] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [showMd, setShowMd] = useState<string | null>(null);

  if (mig.loading && !mig.data) return <Loading />;
  if (!mig.data) return <ErrorBox error={mig.error} title="Migration not found" />;
  const m = mig.data;
  const active = (execs.data ?? []).find((x) => ACTIVE.includes(x.status));
  const ep = (eid: string) => eps.data?.find((e) => e.id === eid);
  const lastReal = (execs.data ?? []).find((x) => !x.dry_run);
  const lastDry = (execs.data ?? []).find((x) => x.dry_run);
  const dryOk = lastDry?.status === "completed";

  const start = async (dry: boolean) => {
    setErr(null);
    const x = await api.post<Execution>(`/migrations/${m.id}/start`, { dry_run: dry });
    nav(`/executions/${x.id}`);
  };
  const r = rep.data;
  const cc = m.crosscheck;

  return (
    <>
      <div className="page-head">
        <div>
          <div className="crumbs"><Link to="/migrations">Migrations</Link> /</div>
          <div className="row"><h1>{m.name}</h1><Status s={m.phase} /></div>
          <div className="sub">{m.vm} · {ep(m.source_endpoint_id)?.name ?? "…"} → {ep(m.target_endpoint_id)?.name ?? "…"}
            {" · "}<Link to={`/logs?migration_id=${m.id}`}>logs</Link>
            {can("admin") && <>{" · "}<Link to={`/audit?object_id=${m.id}`}>audit trail</Link></>}</div>
        </div>
        {can("operator") && (
          <div className="row">
            {active ? <>
              <Link className="btn" to={`/executions/${active.id}`}>View {active.dry_run ? "dry-run" : "run"}</Link>
              <button className="btn danger" onClick={async () => {
                try { await api.post(`/migrations/${m.id}/cancel`); setToast("Cancel requested"); } catch (e) { setErr(errText(e)); }
              }}>Cancel</button>
            </> : <>
              <button className="btn" onClick={() => start(true).catch((e) => setErr(errText(e)))}>Dry-run</button>
              <button className="btn primary" disabled={m.phase === "done" || !dryOk} title={!dryOk ? "Run a dry-run first" : ""}
                      onClick={() => setConfirm(true)}>{lastReal?.status === "failed" ? "Run again" : "Run migration"}</button>
              <button className="btn ghost" onClick={() => setDel(true)}>Delete</button>
            </>}
          </div>
        )}
      </div>
      <ErrorBox error={err} title="Action failed" />
      {active && !active.dry_run && active.transfer && active.transfer.total_bytes > 0 && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="spread" style={{ marginBottom: 8 }}>
            <h2 style={{ margin: 0 }}>Migration in progress</h2>
            <Link className="small" to={`/executions/${active.id}`}>details</Link></div>
          <TransferBar id={active.id} x={active.transfer} status={active.status} />
        </div>)}

      {!dryOk && !active && m.phase !== "done" && <div className="alert info" style={{ marginBottom: 16 }}>
        Run a <b>dry-run</b> first. It validates both endpoints, re-checks the disk counts and shows the full plan without changing anything.</div>}
      {lastReal?.status === "failed" && !active && <div className="alert bad" style={{ marginBottom: 16 }}>
        <b>The last run failed.</b> Fix the cause and <Link to={`/executions/${lastReal.id}`}>retry that execution</Link>: it resumes from where it stopped, and finished disks are not copied again.
        <pre>{lastReal.error}</pre></div>}

      <div className="grid cols-3">
        <div className="card stat"><div className="k">Disks</div><div className="v">{m.disks ?? "—"}</div></div>
        <div className="card stat"><div className="k">Disk-count check</div><div className="v">
          {cc ? (new Set(Object.values(cc)).size === 1 ? <span style={{ color: "var(--ok)" }}>agree</span> : <span style={{ color: "var(--bad)" }}>MISMATCH</span>) : "—"}</div>
          {cc && <div className="small mono muted">{Object.entries(cc).map(([k, v]) => `${k}=${v}`).join(" · ")}</div>}</div>
        <div className="card stat"><div className="k">Target VM</div><div className="v" style={{ fontSize: 16 }}>
          {r?.target_vm ? String((r.target_vm as { name?: string }).name ?? "created") : "—"}</div></div>
      </div>

      <div className="card">
        <h2>Executions</h2>
        {(execs.data ?? []).length === 0 ? <p className="muted">No runs yet.</p> : (
          <div className="table-wrap"><table>
            <thead><tr><th>Started</th><th>Type</th><th>Status</th><th className="num">Tasks</th><th>Duration</th><th>Error</th></tr></thead>
            <tbody>{execs.data!.map((x) => {
              const t = Object.values(x.task_counts).reduce((a, b) => a + b, 0);
              return (
                <tr key={x.id} className="click" onClick={() => nav(`/executions/${x.id}`)}>
                  <td className="small">{fmt.time(x.created_at)}</td>
                  <td>{x.dry_run ? <span className="tag">dry-run</span> : <span className="tag" style={{ borderColor: "var(--accent)", color: "var(--accent)" }}>real</span>}
                    {x.runs > 1 && <span className="muted small"> · {x.runs} attempts</span>}</td>
                  <td><Status s={x.status} /></td>
                  <td className="num">{x.task_counts.completed ?? 0}/{t}</td>
                  <td className="small">{fmt.dur(x.started_at, x.finished_at)}</td>
                  <td className="small" style={{ color: "var(--bad)", maxWidth: 380, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{x.error}</td>
                </tr>);
            })}</tbody>
          </table></div>)}
      </div>

      {r && (
        <div className="card">
          <div className="spread" style={{ marginBottom: 12 }}>
            <h2 style={{ margin: 0 }}>Verification report</h2>
            <div className="row">
              <button className="btn sm" onClick={async () => setShowMd(await api.get<string>(`/migrations/${m.id}/report?format=md`))}>View Markdown</button>
              <button className="btn sm" onClick={() => download(`/migrations/${m.id}/report?format=md`, `${m.name}.report.md`)}>Download .md</button>
              <button className="btn sm" onClick={() => download(`/migrations/${m.id}/report`, `${m.name}.report.json`)}>Download .json</button>
            </div>
          </div>
          <div className="row" style={{ marginBottom: 12 }}>
            {!r.crosscheck
              ? <span className="pill">disk count not checked yet</span>
              : <span className={`pill ${r.disk_count_match ? "ok" : "bad"}`}>disk count {r.disk_count_match ? "matches" : "MISMATCH"}</span>}
            {r.verification && "boot" in r.verification && <span className={`pill ${(r.verification.boot as { ok: boolean }).ok ? "ok" : "bad"}`}>
              boot {(r.verification.boot as { ok: boolean }).ok ? "ok" : "failed"}</span>}
            {r.morph && <span className={`pill ${r.morph.status === "manual_required" ? "warn" : "ok"}`}>OSMorph: {String(r.morph.status)}</span>}
          </div>
          <div className="table-wrap"><table>
            <thead><tr><th>#</th><th>Disk</th><th>Bus</th><th className="num">Size</th><th>Copied</th><th>Committed</th><th>Attached</th><th>Checksums</th></tr></thead>
            <tbody>{r.disks.map((d) => (
              <tr key={d.id}><td className="mono">{d.order}</td><td className="mono small">{d.id}</td><td className="mono">{d.bus}</td>
                <td className="num">{fmt.bytes(d.size_bytes)}</td><td><Check ok={d.copied} /></td><td><Check ok={d.committed} /></td>
                <td><Check ok={d.attached} /></td>
                <td>{d.verify ? <Check ok={!!d.verify.ok} label={d.verify.ok ? "sampled match" : "mismatch"} /> : <span className="muted">—</span>}</td></tr>))}</tbody>
          </table></div>
          {r.errors.length > 0 && (r.phase === "done" ? (
            // finished: errors from earlier attempts were fixed by retries; keep them as history only
            <details className="small" style={{ marginTop: 16 }}>
              <summary className="muted" style={{ cursor: "pointer" }}>
                {r.errors.length} error{r.errors.length > 1 ? "s" : ""} from earlier attempts (resolved by retries; show history)</summary>
              <pre className="mono small" style={{ whiteSpace: "pre-wrap" }}>{r.errors.map((e) => `[${e.phase}] ${e.error}`).join("\n")}</pre>
            </details>
          ) : (<>
            <div className="alert bad" style={{ marginTop: 16 }}><b>Latest error</b>
              <pre style={{ whiteSpace: "pre-wrap" }}>{(() => { const e = r.errors[r.errors.length - 1]; return `[${e.phase}] ${e.error}`; })()}</pre></div>
            {r.errors.length > 1 && <details className="small" style={{ marginTop: 8 }}>
              <summary className="muted" style={{ cursor: "pointer" }}>{r.errors.length - 1} earlier error(s)</summary>
              <pre className="mono small" style={{ whiteSpace: "pre-wrap" }}>{r.errors.slice(0, -1).map((e) => `[${e.phase}] ${e.error}`).join("\n")}</pre>
            </details>}
          </>))}
        </div>
      )}

      <div className="card">
        <h2>Options</h2>
        <pre className="md">{JSON.stringify(m.options, null, 2)}</pre>
      </div>

      {confirm && <ConfirmExecute name={m.name} vm={m.vm} disks={m.disks} onClose={() => setConfirm(false)}
                                  onConfirm={() => start(false)} />}
      {del && <Modal title={`Delete ${m.name}?`} onClose={() => setDel(false)} footer={<>
        <button className="btn" onClick={() => setDel(false)}>Cancel</button>
        <button className="btn danger solid" onClick={async () => {
          try { await api.del(`/migrations/${m.id}`); nav("/migrations"); } catch (e) { setErr(errText(e)); setDel(false); }
        }}>Delete record</button></>}>
        <p style={{ marginTop: 0 }}>Deletes this migration's record and run history from vmigrate. <b>Nothing on the source or target is deleted</b>; any disks or VM it created stay where they are.</p>
      </Modal>}
      {showMd !== null && <Modal title="Report" onClose={() => setShowMd(null)}><pre className="md">{showMd}</pre></Modal>}
      <Toast msg={toast} onDone={() => setToast(null)} />
    </>
  );
}
