// Purpose: Migrations list — every migration with its phase, disk count, and latest execution
// (status, dry-run or real, task progress). Polls every 3 s.

import { useNavigate } from "react-router-dom";
import { api, fmt, type Endpoint, type Migration } from "../api";
import { Bar, ErrorBox, Icon, Loading, Status, TransferBar, useAuth, useLoad } from "../ui";

export default function Migrations() {
  const nav = useNavigate();
  const { can } = useAuth();
  const migs = useLoad(() => api.get<Migration[]>("/migrations"), [], 3000);
  const eps = useLoad(() => api.get<Endpoint[]>("/endpoints"));
  const epName = (id: string) => eps.data?.find((e) => e.id === id)?.name ?? "…";
  const list = migs.data ?? [];
  const active = list.filter((m) => ["pending", "queued", "running"].includes(m.last_execution?.status ?? "")).length;
  const done = list.filter((m) => m.phase === "done").length;
  const failed = list.filter((m) => m.last_execution?.status === "failed").length;

  return (
    <>
      <div className="page-head">
        <div><h1>Migrations</h1><div className="sub">One-shot moves of a VM to another platform, with every disk included.</div></div>
        {can("operator") && <button className="btn primary" onClick={() => nav("/migrations/new")}><Icon n="plus" />New migration</button>}
      </div>
      <div className="grid cols-4" style={{ marginBottom: 16 }}>
        <div className="card stat"><div className="k">Migrations</div><div className="v">{list.length}</div></div>
        <div className="card stat"><div className="k">Running</div><div className="v">{active}</div></div>
        <div className="card stat"><div className="k">Completed</div><div className="v">{done}</div></div>
        <div className="card stat"><div className="k">Last run failed</div><div className="v" style={{ color: failed ? "var(--bad)" : undefined }}>{failed}</div></div>
      </div>
      <ErrorBox error={migs.error} />
      {migs.loading && !migs.data ? <Loading /> : list.length === 0 ? (
        <div className="card empty"><h2>No migrations yet</h2>
          <p>Add a source and a target endpoint, then start the wizard. The first run is always a dry-run.</p>
          {can("operator") && <button className="btn primary" onClick={() => nav("/migrations/new")}>New migration</button>}</div>
      ) : (
        <div className="card"><div className="table-wrap"><table>
          <thead><tr><th>Name</th><th>VM</th><th>Route</th><th className="num">Disks</th><th>Phase</th><th>Latest run</th><th style={{ width: 240 }}>Progress</th><th>Updated</th></tr></thead>
          <tbody>{list.map((m) => {
            const x = m.last_execution;
            const total = x ? Object.values(x.task_counts).reduce((a, b) => a + b, 0) : 0;
            const comp = x?.task_counts.completed ?? 0;
            return (
              <tr key={m.id} className="click" onClick={() => nav(`/migrations/${m.id}`)}>
                <td><b>{m.name}</b></td>
                <td>{m.vm}</td>
                <td className="small">{epName(m.source_endpoint_id)} → {epName(m.target_endpoint_id)}</td>
                <td className="num">{m.disks ?? "—"}</td>
                <td><Status s={m.phase} /></td>
                <td>{x ? <div className="row"><Status s={x.status} />{x.dry_run && <span className="tag">dry-run</span>}</div> : <span className="muted">never</span>}</td>
                <td>{x && !x.dry_run && x.transfer && x.transfer.total_bytes > 0 ? (
                  <TransferBar id={x.id} x={x.transfer} status={x.status} compact />
                ) : x && total ? <div className="row" style={{ flexWrap: "nowrap" }}>
                  <div style={{ flex: 1 }}><Bar pct={(comp / total) * 100} tone={x.status === "failed" ? "bad" : x.status === "completed" ? "ok" : undefined} /></div>
                  <span className="small muted">{comp}/{total}</span></div> : null}</td>
                <td className="small muted">{fmt.ago(m.updated_at)}</td>
              </tr>
            );
          })}</tbody>
        </table></div></div>
      )}
    </>
  );
}
