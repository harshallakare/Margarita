// Purpose: Settings — notification channels (admin: email alerts, with save-time SMTP check and a
// test send), users (admin: list + create), worker pool status, secret store status, and the live
// provider support matrix.

import { useState } from "react";
import { api, fmt, type Channel, type EmailConfig, type Providers } from "../api";
import { ErrorBox, Modal, Status, Toast, errText, useAuth, useLoad } from "../ui";

interface User { id: string; username: string; role: string; project: string; disabled: boolean; created_at: number }
interface WorkerRow { id: string; hostname: string; pid: number; heartbeat_at: number; current_execution: string | null;
  alive: boolean; region?: string | null; capabilities?: string[] | null }

interface Pool { id: string; name: string; region: string | null; description: string | null; min_size: number;
  max_size: number; tag: string; busy: number; last_error: string | null; provision: Record<string, unknown> | null;
  members: { id: string; hostname: string; busy: boolean }[];
  minions: { server_id: string; name: string; status: string; created_at: number }[] }

function NewPool({ onClose, onDone }: { onClose: () => void; onDone: (m: string) => void }) {
  const [name, setName] = useState("");
  const [region, setRegion] = useState("");
  const [desc, setDesc] = useState("");
  const [err, setErr] = useState<string | null>(null);
  return (
    <Modal title="Add worker pool" onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={!name} onClick={async () => {
        try { await api.post("/pools", { name, region: region || null, description: desc || null }); onDone(`Added pool ${name}`); }
        catch (e) { setErr(errText(e)); }
      }}>Add pool</button></>}>
      <div className="form">
        <label className="field"><span>Name</span><input type="text" value={name} onChange={(e) => setName(e.target.value)} />
          <small>Workers join with <code>deploy.sh --pool {name || "NAME"}</code> or an enrollment token.</small></label>
        <label className="field"><span>Region (optional)</span><input type="text" value={region} onChange={(e) => setRegion(e.target.value)} /></label>
        <label className="field"><span>Description</span><input type="text" value={desc} onChange={(e) => setDesc(e.target.value)} /></label>
        <ErrorBox error={err} title="Could not add the pool" />
      </div>
    </Modal>
  );
}

function JoinToken({ pool, onClose }: { pool: Pool; onClose: () => void }) {
  const [tok, setTok] = useState<{ token: string; expires_at: number } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  return (
    <Modal title={`Join pool ${pool.name}`} onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Close</button>
      {!tok && <button className="btn primary" onClick={async () => {
        try { setTok(await api.post(`/pools/${pool.id}/enroll-token`, { ttl_minutes: 60 })); } catch (e) { setErr(errText(e)); }
      }}>Create token</button>}</>}>
      <p className="small">A single-use token, valid for one hour. On the new worker host run (the API must be reachable over HTTPS
        and use a shared Postgres database):</p>
      {tok && <pre className="mono small" style={{ whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
        {`sudo bash deploy.sh --enroll ${window.location.origin} --enroll-token ${tok.token}`}</pre>}
      {tok && <p className="muted small">Expires {fmt.time(tok.expires_at)}. It is shown only once.</p>}
      <ErrorBox error={err} title="Could not create a token" />
    </Modal>
  );
}

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

const EMPTY: EmailConfig = { host: "", port: 587, security: "starttls", username: "", from_addr: "", to: [],
  verify_tls: true, subject_prefix: "[Margarita]" };

function ChannelEditor({ ch, onClose, onDone }: { ch: Channel | null; onClose: () => void; onDone: (m: string) => void }) {
  const [name, setName] = useState(ch?.name ?? "ops-email");
  const [cfg, setCfg] = useState<EmailConfig>({ ...EMPTY, ...(ch?.config ?? {}) });
  const [to, setTo] = useState((ch?.config.to ?? []).join(", "));
  const [pw, setPw] = useState("");
  const [minSev, setMinSev] = useState(ch?.min_severity ?? "warning");
  const [resolved, setResolved] = useState(ch?.notify_resolved ?? true);
  const [success, setSuccess] = useState(ch?.notify_success ?? false);
  const [enabled, setEnabled] = useState(ch?.enabled ?? true);
  const [skip, setSkip] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const set = (k: keyof EmailConfig, v: unknown) => setCfg((c) => ({ ...c, [k]: v }));
  const recipients = to.split(/[,;\s]+/).map((x) => x.trim()).filter(Boolean);
  const ok = name && cfg.host && cfg.port > 0 && cfg.from_addr.includes("@") && recipients.length > 0 && recipients.every((r) => r.includes("@"));
  const save = async () => {
    setBusy(true); setErr(null);
    const config = { ...cfg, to: recipients, username: cfg.username?.trim() || null };
    const common = { config, min_severity: minSev, notify_resolved: resolved, notify_success: success, enabled, skip_check: skip };
    try {
      if (ch) await api.patch(`/notification-channels/${ch.id}`, { ...common, ...(pw ? { password: pw } : {}) });
      else await api.post("/notification-channels", { name, type: "email", ...common, ...(pw ? { password: pw } : {}) });
      onDone(ch ? `Saved ${name}` : `Added ${name}`);
    } catch (e) { setErr(errText(e)); } finally { setBusy(false); }
  };
  return (
    <Modal title={ch ? `Edit ${ch.name}` : "Add email notifications"} onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={!ok || busy} onClick={save}>{busy ? "Checking SMTP…" : ch ? "Save" : "Add channel"}</button></>}>
      <div className="form">
        {!ch && <label className="field"><span>Name</span><input type="text" value={name} onChange={(e) => setName(e.target.value)} /></label>}
        <div className="form cols">
          <label className="field"><span>SMTP server</span>
            <input type="text" value={cfg.host} placeholder="smtp.example.com" onChange={(e) => set("host", e.target.value)} /></label>
          <label className="field"><span>Port</span>
            <input type="number" min={1} max={65535} value={cfg.port} onChange={(e) => set("port", Number(e.target.value))} /></label>
          <label className="field"><span>Security</span>
            <select value={cfg.security} onChange={(e) => { set("security", e.target.value); set("port", e.target.value === "ssl" ? 465 : e.target.value === "none" ? 25 : 587); }}>
              <option value="starttls">STARTTLS (587)</option><option value="ssl">SSL/TLS (465)</option><option value="none">None (25, internal relay)</option></select></label>
          <label className="field"><span>Username (optional)</span>
            <input type="text" value={cfg.username ?? ""} autoComplete="off" onChange={(e) => set("username", e.target.value)} /></label>
          <label className="field"><span>Password{ch?.has_password ? " (leave empty to keep)" : ""}</span>
            <input type="password" value={pw} autoComplete="new-password" onChange={(e) => setPw(e.target.value)} />
            <small>Stored encrypted in the secret store; never shown again.</small></label>
          <label className="field"><span>From</span>
            <input type="text" value={cfg.from_addr} placeholder="margarita@example.com" onChange={(e) => set("from_addr", e.target.value)} /></label>
        </div>
        <label className="field"><span>Send to</span>
          <input type="text" value={to} placeholder="ops@example.com, dr-team@example.com" onChange={(e) => setTo(e.target.value)} />
          <small>Comma-separated addresses.</small></label>
        <div className="form cols">
          <label className="field"><span>Send alerts of</span>
            <select value={minSev} onChange={(e) => setMinSev(e.target.value as "warning" | "critical")}>
              <option value="warning">warning and critical</option><option value="critical">critical only</option></select></label>
          <label className="field"><span>Subject prefix</span>
            <input type="text" value={cfg.subject_prefix} onChange={(e) => set("subject_prefix", e.target.value)} /></label>
        </div>
        <label className="check"><input type="checkbox" checked={resolved} onChange={(e) => setResolved(e.target.checked)} /> Also email when an alert clears</label>
        <label className="check"><input type="checkbox" checked={success} onChange={(e) => setSuccess(e.target.checked)} /> Email when a migration, deploy or cutover completes</label>
        <label className="check"><input type="checkbox" checked={cfg.verify_tls} onChange={(e) => set("verify_tls", e.target.checked)} /> Verify the server's TLS certificate</label>
        <label className="check"><input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> Enabled</label>
        <label className="check"><input type="checkbox" checked={skip} onChange={(e) => setSkip(e.target.checked)} /> Save without checking the SMTP login first</label>
        <ErrorBox error={err} title="Not saved" />
      </div>
    </Modal>);
}

function Notifications({ onToast }: { onToast: (m: string) => void }) {
  const chans = useLoad(() => api.get<Channel[]>("/notification-channels"), [], 15000);
  const [edit, setEdit] = useState<Channel | "new" | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  const test = (c: Channel) => {
    setTesting(c.id); setErr(null);
    api.post<{ sent_to: string[] }>(`/notification-channels/${c.id}/test`)
      .then((r) => onToast(`Test email sent to ${r.sent_to.join(", ")}`))
      .catch((e) => setErr(errText(e))).finally(() => { setTesting(null); chans.reload(); });
  };
  const del = (c: Channel) => {
    if (!confirm(`Delete notification channel ${c.name}?`)) return;
    api.del(`/notification-channels/${c.id}`).then(() => { chans.reload(); onToast(`Deleted ${c.name}`); }).catch((e) => setErr(errText(e)));
  };
  return (
    <div className="card">
      <div className="spread" style={{ marginBottom: 12 }}><h2 style={{ margin: 0 }}>Alert notifications</h2>
        <button className="btn small" onClick={() => setEdit("new")}>Add email</button></div>
      <ErrorBox error={err ?? chans.error} title="Notification channel" />
      {(chans.data ?? []).length === 0 ? <p className="muted">No channels: alerts only show on the Alerts page. Add an email channel to be told
        when a sync fails or a replica misses its recovery point objective.</p> : (
        <div className="table-wrap"><table>
          <thead><tr><th>Channel</th><th>Sends</th><th>Last sent</th><th /></tr></thead>
          <tbody>{chans.data!.map((c) => (
            <tr key={c.id}><td><b>{c.name}</b>{!c.enabled && <span className="tag" style={{ marginLeft: 6 }}>off</span>}
              <div className="muted small">{c.config.host}:{c.config.port} → {c.config.to.join(", ")}</div>
              {c.last_error && <div className="small" style={{ color: "var(--bad)" }}>{c.last_error}</div>}</td>
              <td className="small">{c.min_severity === "critical" ? "critical" : "warning + critical"}
                {c.notify_resolved ? " · cleared" : ""}{c.notify_success ? " · completions" : ""}</td>
              <td className="small muted">{fmt.ago(c.last_sent_at)}</td>
              <td className="row" style={{ justifyContent: "end" }}>
                <button className="btn small" disabled={testing === c.id} onClick={() => test(c)}>{testing === c.id ? "Sending…" : "Send test"}</button>
                <button className="btn small" onClick={() => setEdit(c)}>Edit</button>
                <button className="btn small danger" onClick={() => del(c)}>Delete</button></td></tr>))}</tbody>
        </table></div>)}
      <p className="muted small" style={{ marginBottom: 0 }}>Set <code>VMIGRATE_PUBLIC_URL</code> on the server to get links back to this UI in emails.
        Unacknowledged critical alerts are re-sent daily (<code>VMIGRATE_ALERT_REPEAT_HOURS</code>).</p>
      {edit && <ChannelEditor ch={edit === "new" ? null : edit} onClose={() => setEdit(null)}
                              onDone={(m) => { setEdit(null); chans.reload(); onToast(m); }} />}
    </div>);
}

export default function Settings() {
  const { can } = useAuth();
  const admin = can("admin");
  const users = useLoad(() => (admin ? api.get<User[]>("/users") : Promise.resolve([] as User[])), [admin]);
  const workers = useLoad(() => api.get<WorkerRow[]>("/workers"), [], 5000);
  const secrets = useLoad(() => (admin ? api.get<Record<string, unknown>>("/secrets/status") : Promise.resolve(null)), [admin]);
  const prov = useLoad(() => api.get<Providers>("/providers"));
  const [add, setAdd] = useState(false);
  const pools = useLoad(() => api.get<Pool[]>("/pools"), [], 5000);
  const [addPool, setAddPool] = useState(false);
  const [join, setJoin] = useState<Pool | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  return (
    <>
      <div className="page-head"><div><h1>Settings</h1><div className="sub">Alert notifications, users, workers, secrets and platform support.</div></div></div>
      <div className="grid cols-2" style={{ alignItems: "start" }}>
        {admin && <Notifications onToast={setToast} />}
        <div className="card">
          <div className="spread" style={{ marginBottom: 12 }}><h2 style={{ margin: 0 }}>Workers</h2>
            <span className="muted small">{(workers.data ?? []).filter((w) => w.alive).length} alive</span></div>
          <ErrorBox error={workers.error} />
          {(workers.data ?? []).length === 0 ? <p className="muted">No workers have checked in. Start one with <code>vmigrate-platform worker</code>.</p> : (
            <div className="table-wrap"><table>
              <thead><tr><th>Worker</th><th>Region</th><th>State</th><th>Heartbeat</th></tr></thead>
              <tbody>{workers.data!.map((w) => (
                <tr key={w.id}><td className="mono small">{w.id}
                  <div className="muted small" title={(w.capabilities ?? []).join("\n")}>
                    {(w.capabilities ?? []).filter((c) => !c.startsWith("provider:")).join(" · ") || "—"}</div></td>
                  <td className="small">{w.region || <span className="muted">any</span>}</td>
                  <td>{!w.alive ? <Status s="failed" label="stale" /> : w.current_execution ? <Status s="running" label="busy" /> : <Status s="ok" label="idle" />}</td>
                  <td className="small muted">{fmt.ago(w.heartbeat_at)}</td></tr>))}</tbody>
            </table></div>)}
        </div>
        <div className="card">
          <div className="spread" style={{ marginBottom: 12 }}><h2 style={{ margin: 0 }}>Worker pools</h2>
            {admin && <button className="btn small" onClick={() => setAddPool(true)}>Add pool</button>}</div>
          <ErrorBox error={pools.error} />
          {(pools.data ?? []).length === 0 ? <p className="muted">No pools. Without pools any capable worker runs any job; with them a
            migration can be pinned to a pool (wizard, Options).</p> : (
            <div className="table-wrap"><table>
              <thead><tr><th>Pool</th><th>Members</th><th></th></tr></thead>
              <tbody>{pools.data!.map((p) => (
                <tr key={p.id}><td><b>{p.name}</b><div className="muted small">{p.region ? `region ${p.region}` : "any region"}</div>
                  {p.last_error && <div className="small" style={{ color: "var(--danger, #c33)" }}>{p.last_error}</div>}</td>
                  <td className="small">{p.members.length} live · {p.busy} busy
                    {p.provision && <div className="muted">auto: min {p.min_size} / max {p.max_size} ·{" "}
                      {p.minions.filter((m) => m.status === "booting").length} booting</div>}</td>
                  <td>{admin && <button className="btn small" onClick={() => setJoin(p)}>Join token</button>}</td></tr>))}</tbody>
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
      {addPool && <NewPool onClose={() => setAddPool(false)} onDone={(m) => { setAddPool(false); setToast(m); pools.reload(); }} />}
      {join && <JoinToken pool={join} onClose={() => setJoin(null)} />}
      <Toast msg={toast} onDone={() => setToast(null)} />
    </>
  );
}
