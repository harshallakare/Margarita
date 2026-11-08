// Purpose: Alerts — what is wrong right now (firing, newest first) and what was wrong recently
// (resolved). Operators acknowledge a firing alert to stop its daily email reminders; it stays
// listed until the condition clears on its own (alerts are state-based, never closed by hand).

import { useState } from "react";
import { Link } from "react-router-dom";
import { api, fmt, type Alert } from "../api";
import { ErrorBox, Loading, Toast, errText, useAuth, useLoad } from "../ui";

function Target({ a }: { a: Alert }) {
  if (!a.migration_id) return <span className="muted">—</span>;
  const replica = a.title.startsWith("replica ");
  return <Link to={`/${replica ? "replicas" : "migrations"}/${a.migration_id}`}>{a.object_name ?? a.migration_id.slice(0, 8)}</Link>;
}

function since(a: Alert): string {
  const end = a.resolved_at ?? Date.now() / 1000;
  const m = Math.max(0, Math.round((end - a.first_at) / 60));
  return m < 120 ? `${m} min` : m < 2880 ? `${(m / 60).toFixed(1)} h` : `${(m / 1440).toFixed(1)} days`;
}

export default function Alerts() {
  const { can } = useAuth();
  const firing = useLoad(() => api.get<Alert[]>("/alerts?status=firing"), [], 10000);
  const resolved = useLoad(() => api.get<Alert[]>("/alerts?status=resolved&limit=100"), [], 30000);
  const [err, setErr] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const ack = (a: Alert) => api.post(`/alerts/${a.id}/ack`)
    .then(() => { firing.reload(); setToast("Acknowledged: no more reminders for this alert"); })
    .catch((e) => setErr(errText(e)));

  return (
    <>
      <div className="page-head">
        <div><h1>Alerts</h1>
          <div className="sub">Failed syncs and migrations, missed recovery point objectives, stuck work. Alerts clear on their own
            when the problem is fixed; email goes out when one starts and when it clears
            {can("admin") ? <> (<Link to="/settings">notification channels</Link>)</> : null}.</div></div>
      </div>
      <ErrorBox error={err} title="Action failed" />
      <div className="card" style={{ marginBottom: 16 }}>
        <h2 style={{ marginTop: 0 }}>Firing{firing.data ? ` (${firing.data.length})` : ""}</h2>
        {!firing.data ? <Loading /> : firing.data.length === 0 ? <p className="muted" style={{ margin: 0 }}>Nothing is wrong right now.</p> : (
          <div className="table-wrap"><table>
            <thead><tr><th>Severity</th><th>Alert</th><th>Object</th><th>Since</th><th>Notified</th><th /></tr></thead>
            <tbody>{firing.data.map((a) => (
              <tr key={a.id}>
                <td><span className={`sev ${a.severity}`}>{a.severity}</span></td>
                <td><b>{a.title}</b>{a.detail && <pre className="alert-detail">{a.detail}</pre>}
                  {a.execution_id && <div className="small"><Link to={`/executions/${a.execution_id}`}>execution</Link></div>}</td>
                <td><Target a={a} /></td>
                <td className="small">{since(a)}<div className="muted">{fmt.time(a.first_at)}</div></td>
                <td className="small">{a.ack_at ? <>acknowledged by {a.ack_by}<div className="muted">{fmt.ago(a.ack_at)}</div></> : fmt.ago(a.notified_at)}</td>
                <td>{can("operator") && !a.ack_at && <button className="btn sm" onClick={() => ack(a)}>Acknowledge</button>}</td>
              </tr>))}</tbody>
          </table></div>)}
      </div>
      <div className="card">
        <h2 style={{ marginTop: 0 }}>Resolved</h2>
        {!resolved.data ? <Loading /> : resolved.data.length === 0 ? <p className="muted" style={{ margin: 0 }}>No resolved alerts yet.</p> : (
          <div className="table-wrap"><table>
            <thead><tr><th>Severity</th><th>Alert</th><th>Object</th><th>Lasted</th><th>Resolved</th></tr></thead>
            <tbody>{resolved.data.map((a) => (
              <tr key={a.id}>
                <td><span className={`sev ${a.severity}`}>{a.severity}</span></td>
                <td>{a.title}</td><td><Target a={a} /></td>
                <td className="small">{since(a)}</td><td className="small">{fmt.time(a.resolved_at)}</td>
              </tr>))}</tbody>
          </table></div>)}
      </div>
      <Toast msg={toast} onDone={() => setToast(null)} />
    </>
  );
}
