// Purpose: Replicas list — every DR replica with consistency, last sync (mode, bytes moved),
// schedule (next run), and its latest execution. Polls every 3 s.

import { useNavigate } from "react-router-dom";
import { api, fmt, KIND_LABEL, type Endpoint, type Replica } from "../api";
import { ErrorBox, Icon, Loading, Status, useAuth, useLoad } from "../ui";

export function ConsistencyPill({ r }: { r: Replica }) {
  if (!r.last_sync && !r.current_sync) return <span className="pill plain">never synced</span>;
  if (r.current_sync) return <span className="pill warn" title={r.current_sync.last_error ?? ""}>
    {r.current_sync.last_error ? "sync interrupted" : "syncing"}</span>;
  return r.consistent ? <Status s="ok" label="consistent" /> : <span className="pill warn">inconsistent</span>;
}

export default function Replicas() {
  const nav = useNavigate();
  const { can } = useAuth();
  const reps = useLoad(() => api.get<Replica[]>("/replicas"), [], 3000);
  const eps = useLoad(() => api.get<Endpoint[]>("/endpoints"));
  const epName = (id: string) => eps.data?.find((e) => e.id === id)?.name ?? "…";
  const list = reps.data ?? [];
  const scheduled = list.filter((r) => r.schedules.some((s) => s.enabled)).length;
  const protectedBytes = list.reduce((a, r) => a + (r.last_sync ? r.disk_total_bytes ?? 0 : 0), 0);

  return (
    <>
      <div className="page-head">
        <div><h1>Replicas</h1><div className="sub">Incremental DR copies on the target. Sync on a schedule; deploy a VM from them when needed.</div></div>
        {can("operator") && <button className="btn primary" onClick={() => nav("/migrations/new?mode=replica")}><Icon n="plus" />New replica</button>}
      </div>
      <div className="grid cols-4" style={{ marginBottom: 16 }}>
        <div className="card stat"><div className="k">Replicas</div><div className="v">{list.length}</div></div>
        <div className="card stat"><div className="k">Consistent</div><div className="v">{list.filter((r) => r.consistent && !r.current_sync).length}</div></div>
        <div className="card stat"><div className="k">Scheduled</div><div className="v">{scheduled}</div></div>
        <div className="card stat"><div className="k">Protected data</div><div className="v" style={{ fontSize: 18 }}>{fmt.bytes(protectedBytes)}</div></div>
      </div>
      <ErrorBox error={reps.error} />
      {reps.loading && !reps.data ? <Loading /> : list.length === 0 ? (
        <div className="card empty"><h2>No replicas yet</h2>
          <p>A replica copies a VM's disks to the target, then keeps them up to date with incremental syncs.</p>
          {can("operator") && <button className="btn primary" onClick={() => nav("/migrations/new?mode=replica")}>New replica</button>}</div>
      ) : (
        <div className="card"><div className="table-wrap"><table>
          <thead><tr><th>Name</th><th>VM</th><th>Route</th><th className="num">Disks</th><th>State</th><th>Last sync</th><th>Next scheduled</th><th>Latest run</th></tr></thead>
          <tbody>{list.map((r) => {
            const next = r.schedules.filter((s) => s.enabled && s.next_run_at).map((s) => s.next_run_at!).sort()[0];
            const x = r.last_execution;
            return (
              <tr key={r.id} className="click" onClick={() => nav(`/replicas/${r.id}`)}>
                <td><b className="name">{r.name}</b></td>
                <td className="name">{r.vm}</td>
                <td className="small name">{epName(r.source_endpoint_id)} → {epName(r.target_endpoint_id)}</td>
                <td className="num">{r.disks ?? "—"}</td>
                <td><ConsistencyPill r={r} /></td>
                <td className="small">{r.last_sync ? <>{fmt.ago(r.last_sync.finished)}<div className="muted">{r.last_sync.modes.join("+")} · wrote {fmt.bytes(r.last_sync.bytes_written)}</div></> : <span className="muted">—</span>}</td>
                <td className="small">{next ? fmt.time(next) : <span className="muted">manual</span>}</td>
                <td>{x ? <div className="small"><Status s={x.status} /> <span className="muted">{KIND_LABEL[x.kind] ?? x.kind}{x.dry_run ? " (dry-run)" : ""}</span></div> : <span className="muted">never</span>}</td>
              </tr>
            );
          })}</tbody>
        </table></div></div>
      )}
    </>
  );
}
