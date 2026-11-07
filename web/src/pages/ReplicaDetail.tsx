// Purpose: one replica — state, run controls (sync dry-run / sync now, deploy with restore-point
// choice, cancel, delete disks, delete), sync history (mode + bytes read/written/unchanged), restore
// points, deployments, schedule editor with live cron preview, execution history.

import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, fmt, KIND_LABEL, type Endpoint, type Execution, type Replica, type Schedule } from "../api";
import { ConfirmTyped, ErrorBox, Loading, Modal, Status, Toast, TransferBar, errText, useAuth, useLoad } from "../ui";
import { ConsistencyPill } from "./Replicas";

const ACTIVE = ["pending", "queued", "running"];
const PRESETS: [string, string][] = [["*/15 * * * *", "every 15 minutes"], ["0 * * * *", "hourly"],
  ["0 */4 * * *", "every 4 hours"], ["0 2 * * *", "daily at 02:00 UTC"], ["0 2 * * 1-5", "weekdays at 02:00 UTC"]];

function CronEditor({ onSave, onClose, initial }: { initial?: string; onClose: () => void; onSave: (c: string) => Promise<void> }) {
  const [cron, setCron] = useState(initial ?? "0 */4 * * *");
  const [prev, setPrev] = useState<{ valid: boolean; error?: string; next: number[] } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    const t = setTimeout(() => {
      api.get<{ valid: boolean; error?: string; next: number[] }>(`/schedules/preview?cron=${encodeURIComponent(cron)}`)
        .then(setPrev).catch(() => setPrev(null));
    }, 250);
    return () => clearTimeout(t);
  }, [cron]);
  return (
    <Modal title={initial ? "Edit schedule" : "Add sync schedule"} onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={!prev?.valid} onClick={async () => {
        try { await onSave(cron); } catch (e) { setErr(errText(e)); }
      }}>Save schedule</button></>}>
      <div className="stack">
        <div className="choices">{PRESETS.map(([c, l]) => (
          <button key={c} className={`choice ${cron === c ? "on" : ""}`} onClick={() => setCron(c)}>
            <div className="t">{l}</div><div className="d mono">{c}</div></button>))}</div>
        <label className="field"><span>Cron expression (UTC)</span>
          <input type="text" className="mono" value={cron} onChange={(e) => setCron(e.target.value)} />
          <small>minute hour day-of-month month day-of-week. A sync that is due while another run is active is skipped, never stacked.</small></label>
        {prev && (prev.valid
          ? <div className="small"><b>Next runs:</b><ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
              {prev.next.map((t) => <li key={t}>{fmt.time(t)} <span className="muted">({new Date(t * 1000).toISOString().slice(11, 16)} UTC)</span></li>)}</ul></div>
          : <div className="alert bad small">{prev.error}</div>)}
        <ErrorBox error={err} title="Could not save" />
      </div>
    </Modal>
  );
}

function DeployDialog({ r, onClose, onStarted }: { r: Replica; onClose: () => void; onStarted: (x: Execution) => void }) {
  const [rp, setRp] = useState<string>(r.consistent && !r.current_sync ? "" : r.restore_points[0]?.id ?? "");
  const [name, setName] = useState(`${r.options.target_name || r.vm}-dr`.toLowerCase().replace(/[^a-z0-9._-]+/g, "-"));
  const [dry, setDry] = useState(true);
  const [confirm, setConfirm] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const latestOk = r.consistent && !r.current_sync;
  const go = async () => {
    const x = await api.post<Execution>(`/replicas/${r.id}/deploy`, { dry_run: dry, deploy_name: name, restore_point: rp || null });
    onStarted(x);
  };
  if (confirm) return <ConfirmTyped title="Deploy VM from replica" word={name} action="Deploy" onClose={onClose} onConfirm={go}
    body={<>Clones {r.disks} replica disks{rp ? <> from restore point <code>{rp}</code></> : " from the latest sync"} and creates VM
      <b> {name}</b> on the target. The source is not contacted; the replica keeps syncing afterwards.</>} />;
  return (
    <Modal title="Deploy (failover)" onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={!name || (!rp && !latestOk)} onClick={async () => {
        setErr(null);
        if (dry) { try { await go(); } catch (e) { setErr(errText(e)); } } else setConfirm(true);
      }}>{dry ? "Start dry-run" : "Continue"}</button></>}>
      <div className="form">
        <div className="field"><span>Deploy from</span>
          <div className="choices">
            <button className={`choice ${rp === "" ? "on" : ""}`} disabled={!latestOk} onClick={() => setRp("")}>
              <div className="t">Latest sync</div>
              <div className="d">{latestOk ? fmt.time(r.last_sync?.finished) : "unavailable: replica is mid-sync"}</div></button>
            {r.restore_points.map((p) => (
              <button key={p.id} className={`choice ${rp === p.id ? "on" : ""}`} onClick={() => setRp(p.id)}>
                <div className="t">Restore point</div><div className="d">{fmt.time(p.at)}</div></button>))}
          </div>
          {r.restore_points.length === 0 && <small>This target keeps no restore points, so only the latest consistent sync can be deployed.</small>}
        </div>
        <label className="field"><span>VM name on target</span><input type="text" value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label className="check"><input type="checkbox" checked={dry} onChange={(e) => setDry(e.target.checked)} />
          <span>Dry-run<small>Check the replica and show the plan without creating anything.</small></span></label>
        <ErrorBox error={err} title="Could not start" />
      </div>
    </Modal>
  );
}

function CutoverDialog({ r, onClose, onStarted }: { r: Replica; onClose: () => void; onStarted: (x: Execution) => void }) {
  const [name, setName] = useState(`${r.options.target_name || r.vm}`.toLowerCase().replace(/[^a-z0-9._-]+/g, "-"));
  const [shutdown, setShutdown] = useState(false);
  const [force, setForce] = useState(false);
  const [dry, setDry] = useState(true);
  const [confirm, setConfirm] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const go = async () => {
    const x = await api.post<Execution>(`/replicas/${r.id}/cutover`,
      { dry_run: dry, deploy_name: name, shutdown_source: shutdown, force_power_off: shutdown && force });
    onStarted(x);
  };
  if (confirm) return <ConfirmTyped title="Cut over to the target" word={name} action="Cut over" onClose={onClose} onConfirm={go}
    body={<>{shutdown ? <><b>Shuts down the source VM {r.vm}</b>{force ? " (hard power-off)" : " (guest shutdown)"}, then</> : "Leaves the source running, then"} runs
      a final sync and creates VM <b>{name}</b> on the target from it. The source is never deleted.</>} />;
  return (
    <Modal title="Cutover (planned failover)" onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={!name} onClick={async () => {
        setErr(null);
        if (dry) { try { await go(); } catch (e) { setErr(errText(e)); } } else setConfirm(true);
      }}>{dry ? "Start dry-run" : "Continue"}</button></>}>
      <div className="form">
        <label className="field"><span>VM name on target</span><input type="text" value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label className="check"><input type="checkbox" checked={shutdown} onChange={(e) => setShutdown(e.target.checked)} />
          <span>Shut down the source first<small>Recommended: no writes are lost. Without it the final sync is crash-consistent and later writes stay on the source.</small></span></label>
        {shutdown && <label className="check"><input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />
          <span>Hard power-off<small>Only if the guest cannot shut down cleanly (no guest tools/ACPI).</small></span></label>}
        <label className="check"><input type="checkbox" checked={dry} onChange={(e) => setDry(e.target.checked)} />
          <span>Dry-run<small>Show the plan without touching the source or target.</small></span></label>
        <ErrorBox error={err} title="Could not start" />
      </div>
    </Modal>
  );
}

export default function ReplicaDetail() {
  const { id } = useParams();
  const nav = useNavigate();
  const { can } = useAuth();
  const rep = useLoad(() => api.get<Replica>(`/replicas/${id}`), [id], 3000);
  const execs = useLoad(() => api.get<Execution[]>(`/executions?migration_id=${id}`), [id], 3000);
  const eps = useLoad(() => api.get<Endpoint[]>("/endpoints"));
  const [modal, setModal] = useState<null | "sync" | "deploy" | "cutover" | "deldisks" | "delete" | "sched" | Schedule>(null);
  const [err, setErr] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  if (rep.loading && !rep.data) return <Loading />;
  if (!rep.data) return <ErrorBox error={rep.error} title="Replica not found" />;
  const r = rep.data;
  const active = (execs.data ?? []).find((x) => ACTIVE.includes(x.status));
  const ep = (eid: string) => eps.data?.find((e) => e.id === eid);
  const hasDisks = !!r.last_sync || !!r.current_sync;
  const dryOk = (execs.data ?? []).some((x) => x.kind === "replica_sync" && x.dry_run && x.status === "completed");
  const start = async (path: string, body: unknown) => {
    const x = await api.post<Execution>(`/replicas/${r.id}/${path}`, body);
    nav(`/executions/${x.id}`);
  };
  const act = (fn: () => Promise<unknown>) => fn().catch((e) => setErr(errText(e)));

  return (
    <>
      <div className="page-head">
        <div>
          <div className="crumbs"><Link to="/replicas">Replicas</Link> /</div>
          <div className="row"><h1>{r.name}</h1><ConsistencyPill r={r} /></div>
          <div className="sub">{r.vm} · {ep(r.source_endpoint_id)?.name ?? "…"} → {ep(r.target_endpoint_id)?.name ?? "…"} · keeps {r.options.retention ?? 3} restore points</div>
        </div>
        {can("operator") && <div className="row">
          {active ? <>
            <Link className="btn" to={`/executions/${active.id}`}>View {KIND_LABEL[active.kind]?.toLowerCase() ?? "run"}</Link>
            <button className="btn danger" onClick={() => act(async () => { await api.post(`/replicas/${r.id}/cancel`); setToast("Cancel requested"); })}>Cancel</button>
          </> : <>
            <button className="btn" onClick={() => act(() => start("execute", { dry_run: true }))}>Dry-run sync</button>
            <button className="btn" disabled={!dryOk && !hasDisks} title={!dryOk && !hasDisks ? "Run a dry-run first" : ""}
                    onClick={() => setModal("sync")}>Sync now</button>
            <button className="btn" disabled={!r.last_sync} onClick={() => setModal("deploy")}>Deploy…</button>
            <button className="btn primary" disabled={!r.last_sync} onClick={() => setModal("cutover")}>Cutover…</button>
          </>}
        </div>}
      </div>
      <ErrorBox error={err} title="Action failed" />
      {r.current_sync?.last_error && !active && <div className="alert bad" style={{ marginBottom: 16 }}>
        <b>Sync {r.current_sync.id} was interrupted</b> ({r.current_sync.disks_done}/{r.disks} disks done). The replica disks
        are inconsistent until it finishes. <b>Sync now</b> resumes it with the same source snapshot.<pre>{r.current_sync.last_error}</pre></div>}
      {!hasDisks && !active && <div className="alert info" style={{ marginBottom: 16 }}>
        Start with a <b>dry-run sync</b>. The first real sync copies every disk; later syncs copy only what changed.</div>}

      {active && !active.dry_run && active.transfer && active.transfer.total_bytes > 0 && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="spread" style={{ marginBottom: 8 }}>
            <h2 style={{ margin: 0 }}>{KIND_LABEL[active.kind] ?? "Sync"} in progress</h2>
            <Link className="small" to={`/executions/${active.id}`}>details</Link></div>
          <TransferBar id={active.id} x={active.transfer} status={active.status} />
        </div>)}
      <div className="grid cols-4">
        <div className="card stat"><div className="k">Disks</div><div className="v">{r.disks ?? "—"}<small> · {fmt.bytes(r.disk_total_bytes)}</small></div>
          {r.crosscheck && <div className="small mono muted">{new Set(Object.values(r.crosscheck)).size === 1 ? "counts agree" : "COUNT MISMATCH"}</div>}</div>
        <div className="card stat"><div className="k">Last sync</div><div className="v" style={{ fontSize: 17 }}>{r.last_sync ? fmt.ago(r.last_sync.finished) : "—"}</div>
          {r.last_sync && <div className="small muted">{r.last_sync.modes.join("+")} · wrote {fmt.bytes(r.last_sync.bytes_written)}</div>}</div>
        <div className="card stat"><div className="k">Next scheduled</div><div className="v" style={{ fontSize: 17 }}>
          {(() => { const n = r.schedules.filter((s) => s.enabled && s.next_run_at).map((s) => s.next_run_at!).sort()[0];
            return n ? fmt.time(n) : "manual"; })()}</div></div>
        <div className="card stat"><div className="k">Deployments</div><div className="v">{r.deployments.length}</div></div>
      </div>

      <div className="split">
        <div className="card">
          <h2>Sync history</h2>
          {r.syncs.length === 0 ? <p className="muted">No completed syncs.</p> : (
            <div className="table-wrap"><table>
              <thead><tr><th>Finished</th><th>Mode</th><th className="num">Read</th><th className="num">Written</th><th className="num">Unchanged</th><th>Took</th></tr></thead>
              <tbody>{r.syncs.map((s) => (
                <tr key={s.id}><td className="small">{fmt.time(s.finished)}{s.restore_point && <span className="tag" style={{ marginLeft: 6 }}>RP</span>}</td>
                  <td>{s.modes.map((m) => <span key={m} className="tag" style={{ marginRight: 4 }}>{m}</span>)}</td>
                  <td className="num">{fmt.bytes(s.bytes_read)}</td><td className="num">{fmt.bytes(s.bytes_written)}</td>
                  <td className="num muted">{fmt.bytes(s.bytes_unchanged)}</td><td className="small">{fmt.dur(s.started, s.finished)}</td></tr>))}</tbody>
            </table></div>)}
        </div>
        <div className="stack" style={{ marginTop: 0 }}>
          <div className="card">
            <div className="spread" style={{ marginBottom: 8 }}><h2 style={{ margin: 0 }}>Schedules</h2>
              {can("operator") && <button className="btn sm" onClick={() => setModal("sched")}>Add</button>}</div>
            {r.schedules.length === 0 ? <p className="muted small" style={{ margin: 0 }}>Manual only. Add a cron schedule (UTC) to sync automatically.</p> :
              r.schedules.map((s) => (
                <div key={s.id} className="spread" style={{ padding: "8px 0", borderTop: "1px solid var(--border)" }}>
                  <div><code>{s.cron}</code> {!s.enabled && <span className="pill plain">paused</span>}
                    <div className="small muted">{s.enabled && s.next_run_at ? `next ${fmt.time(s.next_run_at)}` : "not scheduled"}
                      {s.last_run_at ? ` · last ${fmt.ago(s.last_run_at)}` : ""}</div>
                    {s.last_skip_reason && <div className="small" style={{ color: "var(--warn)" }}>{s.last_skip_reason}</div>}</div>
                  {can("operator") && <div className="row" style={{ flexWrap: "nowrap" }}>
                    <button className="btn sm" onClick={() => act(async () => { await api.patch(`/schedules/${s.id}`, { enabled: !s.enabled }); rep.reload(); })}>{s.enabled ? "Pause" : "Resume"}</button>
                    <button className="btn sm" onClick={() => setModal(s)}>Edit</button>
                    <button className="btn sm danger" onClick={() => act(async () => { await api.del(`/schedules/${s.id}`); rep.reload(); })}>✕</button>
                  </div>}
                </div>))}
          </div>
          <div className="card">
            <h2>Restore points</h2>
            {r.restore_points.length === 0 ? <p className="muted small" style={{ margin: 0 }}>{r.last_sync ? "This target keeps no restore points; deploys use the latest consistent sync." : "Created after each sync on targets that support it."}</p> :
              <table><tbody>{r.restore_points.map((p, i) => (
                <tr key={p.id}><td className="small">{fmt.time(p.at)}{i === 0 && <span className="tag" style={{ marginLeft: 6 }}>newest</span>}</td>
                  <td className="mono small muted">{p.id}</td></tr>))}</tbody></table>}
          </div>
        </div>
      </div>

      <div className="card">
        <h2>Deployments</h2>
        {r.deployments.length === 0 ? <p className="muted">No VMs deployed from this replica yet.</p> : (
          <div className="table-wrap"><table>
            <thead><tr><th>VM</th><th>From</th><th>When</th><th>Status</th></tr></thead>
            <tbody>{r.deployments.map((d) => (
              <tr key={d.id}><td><b>{d.name}</b>{d.target_vm && <div className="mono small muted">{d.target_vm.id}</div>}</td>
                <td className="small">{d.restore_point ? <>restore point <code>{d.restore_point}</code></> : "latest sync"}</td>
                <td className="small">{fmt.time(d.at)}</td><td><Status s={d.status === "done" ? "completed" : d.status} label={d.status} /></td></tr>))}</tbody>
          </table></div>)}
      </div>

      <div className="card">
        <h2>Executions</h2>
        {(execs.data ?? []).length === 0 ? <p className="muted">No runs yet.</p> : (
          <div className="table-wrap"><table>
            <thead><tr><th>Started</th><th>Kind</th><th>Status</th><th className="num">Tasks</th><th>Duration</th><th>Error</th></tr></thead>
            <tbody>{execs.data!.slice(0, 30).map((x) => {
              const t = Object.values(x.task_counts).reduce((a, b) => a + b, 0);
              return <tr key={x.id} className="click" onClick={() => nav(`/executions/${x.id}`)}>
                <td className="small">{fmt.time(x.created_at)}</td>
                <td className="small">{KIND_LABEL[x.kind] ?? x.kind}{x.dry_run && <span className="tag" style={{ marginLeft: 6 }}>dry-run</span>}
                  {x.params && "schedule_id" in x.params && <span className="tag" style={{ marginLeft: 6 }}>scheduled</span>}</td>
                <td><Status s={x.status} /></td><td className="num">{x.task_counts.completed ?? 0}/{t}</td>
                <td className="small">{fmt.dur(x.started_at, x.finished_at)}</td>
                <td className="small" style={{ color: "var(--bad)", maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{x.error}</td></tr>;
            })}</tbody>
          </table></div>)}
      </div>

      {can("operator") && !active && <div className="card">
        <h2>Danger zone</h2>
        <div className="spread">
          <div className="small">{hasDisks ? "Delete the replica disks and restore points on the target and the base snapshot on the source. Deployed VMs are kept." : "No replica disks exist on the target."}</div>
          <div className="row">
            {hasDisks && <button className="btn danger" onClick={() => setModal("deldisks")}>Delete replica disks…</button>}
            <button className="btn danger" disabled={hasDisks} title={hasDisks ? "Delete the replica disks first" : ""} onClick={() => setModal("delete")}>Delete replica</button>
          </div>
        </div>
      </div>}

      {modal === "sync" && <ConfirmTyped title="Sync now" word={r.name} action="Start sync" danger={false} onClose={() => setModal(null)}
        onConfirm={() => start("execute", { dry_run: false })}
        body={<>Snapshots <b>{r.vm}</b> on the source and updates the {r.disks ?? ""} replica disks on the target
          {r.last_sync ? " with what changed since the last sync" : " (first sync: every disk is copied in full)"}. The source VM is never modified.</>} />}
      {modal === "cutover" && <CutoverDialog r={r} onClose={() => setModal(null)} onStarted={(x) => nav(`/executions/${x.id}`)} />}
      {modal === "deploy" && <DeployDialog r={r} onClose={() => setModal(null)} onStarted={(x) => nav(`/executions/${x.id}`)} />}
      {modal === "deldisks" && <ConfirmTyped title="Delete replica disks" word={r.name} action="Delete disks" onClose={() => setModal(null)}
        onConfirm={() => start("delete-disks", { dry_run: false })}
        body={<>Permanently deletes this replica's {r.disks} disks and {r.restore_points.length} restore points on the target,
          and its base snapshot on the source. VMs already deployed from it are <b>not</b> deleted. This cannot be undone.</>} />}
      {modal === "delete" && <Modal title={`Delete ${r.name}?`} onClose={() => setModal(null)} footer={<>
        <button className="btn" onClick={() => setModal(null)}>Cancel</button>
        <button className="btn danger solid" onClick={() => act(async () => { await api.del(`/replicas/${r.id}`); nav("/replicas"); })}>Delete record</button></>}>
        <p style={{ marginTop: 0 }}>Removes the replica record, its schedules and run history from vmigrate.</p></Modal>}
      {(modal === "sched" || (modal && typeof modal === "object")) && (
        <CronEditor initial={typeof modal === "object" ? modal.cron : undefined} onClose={() => setModal(null)} onSave={async (c) => {
          if (typeof modal === "object" && modal) await api.patch(`/schedules/${modal.id}`, { cron: c });
          else await api.post(`/replicas/${r.id}/schedules`, { cron: c });
          setModal(null); setToast("Schedule saved"); rep.reload();
        }} />)}
      <Toast msg={toast} onDone={() => setToast(null)} />
    </>
  );
}
