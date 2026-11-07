// Purpose: Settings — users (admin: list + create), worker pool status, secret store status,
// and the live provider support matrix.

import { useState } from "react";
import { api, fmt, type Providers } from "../api";
import { ErrorBox, Modal, Status, Toast, errText, useAuth, useLoad } from "../ui";

interface User { id: string; username: string; role: string; project: string; disabled: boolean; created_at: number }
interface WorkerRow { id: string; hostname: string; pid: number; heartbeat_at: number; current_execution: string | null; alive: boolean }

function NewUser({ onClose, onDone }: { onClose: () => void; onDone: (m: string) => void }) {
  const [u, setU] = useState("");
  const [p, setP] = useState("");
  const [role, setRole] = useState("operator");
  const [project, setProject] = useState("default");
  const [err, setErr] = useState<string | null>(null);
  return (
    <Modal title="Add user" onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={!u || p.length < 12} onClick={async () => {
        try { await api.post("/users", { username: u, password: p, role, project }); onDone(`Added ${u}`); }
        catch (e) { setErr(errText(e)); }
      }}>Add user</button></>}>
      <div className="form cols">
        <label className="field"><span>Username</span><input type="text" value={u} onChange={(e) => setU(e.target.value)} /></label>
        <label className="field"><span>Password</span><input type="password" value={p} autoComplete="new-password"
          onChange={(e) => setP(e.target.value)} /><small>At least 12 characters.</small></label>
        <label className="field"><span>Role</span><select value={role} onChange={(e) => setRole(e.target.value)}>
          <option value="viewer">viewer: read only</option><option value="operator">operator: run migrations</option>
          <option value="admin">admin: everything + users</option></select></label>
        <label className="field"><span>Project</span><input type="text" value={project} onChange={(e) => setProject(e.target.value)} /></label>
        <div className="wide"><ErrorBox error={err} title="Could not add user" /></div>
      </div>
    </Modal>
  );
}

export default function Settings() {
  const { can } = useAuth();
  const admin = can("admin");
  const users = useLoad(() => (admin ? api.get<User[]>("/users") : Promise.resolve([] as User[])), [admin]);
  const workers = useLoad(() => api.get<WorkerRow[]>("/workers"), [], 5000);
  const secrets = useLoad(() => (admin ? api.get<Record<string, unknown>>("/secrets/status") : Promise.resolve(null)), [admin]);
  const prov = useLoad(() => api.get<Providers>("/providers"));
  const [add, setAdd] = useState(false);
  const [toast, setToast] = useState<string | null>(null);

  return (
    <>
      <div className="page-head"><div><h1>Settings</h1><div className="sub">Users, workers, secrets and platform support.</div></div></div>
      <div className="grid cols-2" style={{ alignItems: "start" }}>
        <div className="card">
          <div className="spread" style={{ marginBottom: 12 }}><h2 style={{ margin: 0 }}>Workers</h2>
            <span className="muted small">{(workers.data ?? []).filter((w) => w.alive).length} alive</span></div>
          <ErrorBox error={workers.error} />
          {(workers.data ?? []).length === 0 ? <p className="muted">No workers have checked in. Start one with <code>vmigrate-platform worker</code>.</p> : (
            <div className="table-wrap"><table>
              <thead><tr><th>Worker</th><th>State</th><th>Heartbeat</th></tr></thead>
              <tbody>{workers.data!.map((w) => (
                <tr key={w.id}><td className="mono small">{w.id}</td>
                  <td>{!w.alive ? <Status s="failed" label="stale" /> : w.current_execution ? <Status s="running" label="busy" /> : <Status s="ok" label="idle" />}</td>
                  <td className="small muted">{fmt.ago(w.heartbeat_at)}</td></tr>))}</tbody>
            </table></div>)}
        </div>
        <div className="card">
          <h2>Secret store</h2>
          {!admin ? <p className="muted">Visible to admins.</p> : secrets.data ? (
            <dl className="kv"><dt>Backend</dt><dd>{String(secrets.data.backend)}</dd>
              <dt>Keys configured</dt><dd>{String(secrets.data.keys_configured)}</dd>
              <dt>Stored secrets</dt><dd>{String(secrets.data.secrets)}</dd>
              <dt>Encryption</dt><dd className="small">Fernet (AES-128-CBC + HMAC-SHA256). The key is outside the database; credentials are never returned by the API.</dd></dl>
          ) : <ErrorBox error={secrets.error} />}
        </div>
      </div>

      {admin && (
        <div className="card">
          <div className="spread" style={{ marginBottom: 12 }}><h2 style={{ margin: 0 }}>Users</h2>
            <button className="btn sm primary" onClick={() => setAdd(true)}>Add user</button></div>
          <ErrorBox error={users.error} />
          <div className="table-wrap"><table>
            <thead><tr><th>User</th><th>Role</th><th>Project</th><th>Created</th></tr></thead>
            <tbody>{(users.data ?? []).map((u) => (
              <tr key={u.id}><td><b>{u.username}</b>{u.disabled && <span className="muted"> (disabled)</span>}</td>
                <td><span className="tag">{u.role}</span></td><td>{u.project}</td><td className="small muted">{fmt.time(u.created_at)}</td></tr>))}</tbody>
          </table></div>
        </div>
      )}

      <div className="card">
        <h2>Platform support</h2>
        <div className="grid cols-2">
          {(["sources", "targets"] as const).map((side) => (
            <div key={side}><h3>{side}</h3>
              <table><tbody>{(prov.data?.[side] ?? []).filter((r) => r.type !== "fake").map((r) => (
                <tr key={r.type}><td><b>{r.type}</b><div className="muted small">{r.notes}</div></td>
                  <td style={{ textAlign: "right" }}>{r.status === "implemented" ? <Status s="ok" label="implemented" /> : <Status s="pending" label={r.status} />}</td></tr>))}
              </tbody></table></div>))}
        </div>
      </div>
      {add && <NewUser onClose={() => setAdd(false)} onDone={(m) => { setAdd(false); setToast(m); users.reload(); }} />}
      <Toast msg={toast} onDone={() => setToast(null)} />
    </>
  );
}
