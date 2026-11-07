// Purpose: the audit trail (admin) — who did what to which record, when, from where, and whether it
// worked. Filters, row details (request body with secrets masked), chain verification, CSV export.

import { Fragment, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, download, fmt } from "../api";
import { ErrorBox, Loading, Status, useLoad } from "../ui";

interface AuditRow { id: number; ts: number; actor: string; actor_role: string | null; ip: string | null; user_agent: string | null;
  action: string; object_type: string | null; object_id: string | null; object_name: string | null; project: string | null;
  method: string | null; path: string | null; status: number | null; outcome: string; request_id: string | null;
  details: Record<string, unknown> | null; hash: string }

const TYPES = ["", "auth", "user", "endpoint", "migration", "replica", "execution", "schedule", "pool", "minion", "worker", "audit"];
const RANGES: [string, number][] = [["last 24 h", 86400], ["last 7 days", 7 * 86400], ["last 30 days", 30 * 86400], ["all time", 0]];

export default function Audit() {
  const [params] = useSearchParams();
  const [actor, setActor] = useState("");
  const [typ, setTyp] = useState(params.get("object_type") ?? "");
  const [objectId] = useState(params.get("object_id") ?? "");
  const [outcome, setOutcome] = useState("");
  const [q, setQ] = useState("");
  const [range, setRange] = useState(7 * 86400);
  const [open, setOpen] = useState<number | null>(null);
  const query = useMemo(() => {
    const p = new URLSearchParams({ limit: "500" });
    if (actor.trim()) p.set("actor", actor.trim());
    if (typ === "auth") p.set("action", "auth."); else if (typ) p.set("object_type", typ);
    if (objectId) p.set("object_id", objectId);
    if (outcome) p.set("outcome", outcome);
    if (q.trim()) p.set("q", q.trim());
    if (range) p.set("since", String(Date.now() / 1000 - range));
    return p.toString();
  }, [actor, typ, objectId, outcome, q, range]);
  const rows = useLoad(() => api.get<AuditRow[]>(`/audit?${query}`), [query], 15000);
  const [verify, setVerify] = useState<{ ok: boolean; rows: number; broken_at?: number | null; reason?: string } | null>(null);

  return (
    <>
      <div className="page-head">
        <div><h1>Audit trail</h1>
          <div className="sub">Every change made through the API or by the platform itself, including refused attempts and logins.
            Entries cannot be edited; each one is chained to the previous one so tampering is detectable.</div></div>
        <div className="row">
          <button className="btn" onClick={async () => setVerify(await api.get("/audit/verify"))}>Verify integrity</button>
          <button className="btn" onClick={() => download(`/audit/export${range ? `?since=${Date.now() / 1000 - range}` : ""}`, "margarita-audit.csv")}>Export CSV</button>
        </div>
      </div>
      {verify && <div className={`alert ${verify.ok ? "ok" : "bad"}`} style={{ marginBottom: 12 }}>
        {verify.ok ? <><b>Intact.</b> {verify.rows} entries checked; the chain is unbroken.</>
          : <><b>Integrity check failed</b> at entry {verify.broken_at ?? "(end)"}: {verify.reason}.</>}</div>}
      {objectId && <div className="alert info" style={{ marginBottom: 12 }}>Showing entries for record <code>{objectId}</code>.</div>}
      <div className="card" style={{ marginBottom: 12 }}>
        <div className="row" style={{ gap: 12, flexWrap: "wrap", alignItems: "end" }}>
          <label className="field" style={{ flex: "1 1 160px" }}><span>Who</span>
            <input type="text" value={actor} placeholder="username or system:conductor" onChange={(e) => setActor(e.target.value)} /></label>
          <label className="field" style={{ flex: "1 1 140px" }}><span>Record type</span>
            <select value={typ} onChange={(e) => setTyp(e.target.value)}>
              {TYPES.map((t) => <option key={t} value={t}>{t ? (t === "auth" ? "logins" : t) : "all"}</option>)}</select></label>
          <label className="field" style={{ flex: "1 1 120px" }}><span>Outcome</span>
            <select value={outcome} onChange={(e) => setOutcome(e.target.value)}>
              <option value="">all</option><option value="success">success</option><option value="denied">denied</option>
              <option value="failed">failed</option></select></label>
          <label className="field" style={{ flex: "2 1 200px" }}><span>Name or path</span>
            <input type="text" value={q} placeholder="e.g. RIS-Sainath" onChange={(e) => setQ(e.target.value)} /></label>
          <label className="field" style={{ flex: "1 1 130px" }}><span>Time</span>
            <select value={range} onChange={(e) => setRange(Number(e.target.value))}>
              {RANGES.map(([l, s]) => <option key={l} value={s}>{l}</option>)}</select></label>
        </div>
      </div>
      <div className="card">
        <ErrorBox error={rows.error} />
        {rows.loading && !rows.data ? <Loading /> : (rows.data ?? []).length === 0 ? <p className="muted">No entries match.</p> : (
          <div className="table-wrap"><table>
            <thead><tr><th style={{ width: 150 }}>When</th><th>Who</th><th>Action</th><th>Record</th><th>Result</th><th>From</th></tr></thead>
            <tbody>{rows.data!.map((r) => (<Fragment key={r.id}>
              <tr key={r.id} className="click" onClick={() => setOpen(open === r.id ? null : r.id)}>
                <td className="small mono">{new Date(r.ts * 1000).toLocaleString()}<div className="muted">{fmt.ago(r.ts)}</div></td>
                <td className="small"><b>{r.actor}</b><div className="muted">{r.actor_role ?? ""}</div></td>
                <td className="small mono">{r.action}</td>
                <td className="small">{r.object_name ?? <span className="muted">—</span>}
                  <div className="muted mono">{r.object_type}{r.object_id ? ` · ${r.object_id.slice(0, 12)}` : ""}</div></td>
                <td><Status s={r.outcome === "success" ? "completed" : r.outcome === "denied" ? "canceled" : "failed"} label={r.outcome} />
                  {r.status != null && <div className="muted small">HTTP {r.status}</div>}</td>
                <td className="small muted">{r.ip ?? "—"}</td>
              </tr>
              {open === r.id && <tr key={`${r.id}-d`}><td colSpan={6}>
                <div className="small"><b>{r.method} {r.path}</b> · request {r.request_id ?? "—"} · project {r.project ?? "—"}</div>
                {r.user_agent && <div className="small muted">{r.user_agent}</div>}
                <pre className="mono small" style={{ whiteSpace: "pre-wrap", margin: "8px 0 0" }}>{JSON.stringify(r.details ?? {}, null, 2)}</pre>
                <div className="small muted mono">chain {r.hash.slice(0, 16)}…</div>
              </td></tr>}
            </Fragment>))}</tbody>
          </table></div>)}
      </div>
    </>
  );
}
