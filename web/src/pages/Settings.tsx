// Purpose: Settings — notification channels (admin: email alerts with provider presets: Gmail,
// Microsoft 365 via Graph or SMTP, Outlook.com, Yahoo, iCloud, Zoho, SES, SendGrid, ...; save-time
// login check, and a test send before or after saving), your account (change password), users (admin: list, create,
// role / disable / set password), workers (autoscaling, WorkersCard.tsx), worker pools, secret store status, and the live
// provider support matrix.

import { useState } from "react";
import { api, fmt, type Channel, type EmailConfig, type EmailProvider, type Providers } from "../api";
import { ErrorBox, Modal, Status, Toast, errText, useAuth, useLoad } from "../ui";
import { AccountCard, UserEdit, type User } from "./Account";
import WorkersCard from "./WorkersCard";

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

const EMPTY: EmailConfig = { provider: "gmail", host: "", port: 587, security: "starttls", username: "", from_addr: "",
  to: [], verify_tls: true, subject_prefix: "[Margarita]", tenant_id: "", client_id: "" };

function ChannelEditor({ ch, providers, onClose, onDone }: {
  ch: Channel | null; providers: EmailProvider[]; onClose: () => void; onDone: (m: string) => void;
}) {
  const [name, setName] = useState(ch?.name ?? "ops-email");
  const [cfg, setCfg] = useState<EmailConfig>(() => {
    if (ch) return { ...EMPTY, ...ch.config, provider: ch.config.provider || "custom" };
    const g = providers.find((p) => p.id === EMPTY.provider);
    return { ...EMPTY, host: g?.host ?? "", port: g?.port ?? 587, security: g?.security ?? "starttls" };
  });
  const [to, setTo] = useState((ch?.config.to ?? []).join(", "));
  const [pw, setPw] = useState("");
  const [minSev, setMinSev] = useState(ch?.min_severity ?? "warning");
  const [resolved, setResolved] = useState(ch?.notify_resolved ?? true);
  const [success, setSuccess] = useState(ch?.notify_success ?? false);
  const [enabled, setEnabled] = useState(ch?.enabled ?? true);
  const [skip, setSkip] = useState(false);
  const [busy, setBusy] = useState<"save" | "test" | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [tested, setTested] = useState<string | null>(null);
  const [errFrom, setErrFrom] = useState<"save" | "test">("save");
  const prov = providers.find((p) => p.id === cfg.provider) ?? providers.find((p) => p.id === "custom");
  const graph = prov?.kind === "graph";
  const set = (k: keyof EmailConfig, v: unknown) => { setTested(null); setCfg((c) => ({ ...c, [k]: v })); };
  const pick = (id: string) => {
    const p = providers.find((x) => x.id === id);
    setTested(null); setErr(null);
    setCfg((c) => ({ ...c, provider: id, host: p?.kind === "smtp" ? p.host ?? "" : "", port: p?.port ?? 587,
                     security: p?.security ?? "starttls", username: p?.username ?? "" }));
  };
  const recipients = to.split(/[,;\s]+/).map((x) => x.trim()).filter(Boolean);
  const hasSecret = !!pw || !!ch?.has_password;
  const ok = !!name && cfg.from_addr.includes("@") && recipients.length > 0 && recipients.every((r) => r.includes("@"))
    && (graph ? !!cfg.tenant_id && !!cfg.client_id && hasSecret : !!cfg.host && cfg.port > 0);
  const config = () => {
    const user = (cfg.username ?? "").trim() || (prov?.username_is_from ? cfg.from_addr.trim() : "");
    return { ...cfg, to: recipients, username: graph ? null : user || null,
             tenant_id: graph ? (cfg.tenant_id ?? "").trim() : null, client_id: graph ? (cfg.client_id ?? "").trim() : null };
  };
  const save = async () => {
    setBusy("save"); setErr(null);
    const common = { config: config(), min_severity: minSev, notify_resolved: resolved, notify_success: success, enabled, skip_check: skip };
    try {
      if (ch) await api.patch(`/notification-channels/${ch.id}`, { ...common, ...(pw ? { password: pw } : {}) });
      else await api.post("/notification-channels", { name, type: "email", ...common, ...(pw ? { password: pw } : {}) });
      onDone(ch ? `Saved ${name}` : `Added ${name}`);
    } catch (e) { setErrFrom("save"); setErr(errText(e)); } finally { setBusy(null); }
  };
  const test = async () => {
    setBusy("test"); setErr(null); setTested(null);
    try {
      const r = await api.post<{ sent_to: string[] }>("/notification-channels/test",
        { config: config(), ...(pw ? { password: pw } : {}), ...(ch ? { channel_id: ch.id } : {}) });
      setTested(`Test email sent to ${r.sent_to.join(", ")}. Check the inbox (and the spam folder).`);
    } catch (e) { setErrFrom("test"); setErr(errText(e)); } finally { setBusy(null); }
  };
  const userFixed = !!prov?.username;
  return (
    <Modal title={ch ? `Edit ${ch.name}` : "Add email notifications"} onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn" disabled={!ok || !!busy} onClick={test}
              title="Send one test email with these settings, without saving them">{busy === "test" ? "Sending…" : "Send test email"}</button>
      <button className="btn primary" disabled={!ok || !!busy} onClick={save}>
        {busy === "save" ? (graph ? "Signing in…" : "Checking login…") : ch ? "Save" : "Add channel"}</button></>}>
      <div className="form">
        <div className="form cols">
          {!ch && <label className="field"><span>Name</span><input type="text" value={name} onChange={(e) => setName(e.target.value)} /></label>}
          <label className="field"><span>Email provider</span>
            <select value={cfg.provider} onChange={(e) => pick(e.target.value)}>
              {providers.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}</select></label>
        </div>
        {prov && <div className="alert info small">{prov.help}</div>}
        {prov?.warning && <div className="alert warn small">{prov.warning}</div>}
        {graph ? <div className="form cols">
          <label className="field"><span>Directory (tenant) ID</span>
            <input type="text" value={cfg.tenant_id ?? ""} placeholder="00000000-0000-0000-0000-000000000000 or contoso.onmicrosoft.com"
                   onChange={(e) => set("tenant_id", e.target.value)} /></label>
          <label className="field"><span>Application (client) ID</span>
            <input type="text" value={cfg.client_id ?? ""} placeholder="00000000-0000-0000-0000-000000000000"
                   onChange={(e) => set("client_id", e.target.value)} /></label>
          <label className="field"><span>{prov?.secret_label}{ch?.has_password ? " (leave empty to keep)" : ""}</span>
            <input type="password" value={pw} autoComplete="new-password" onChange={(e) => { setTested(null); setPw(e.target.value); }} />
            <small>Stored encrypted in the secret store; never shown again. Client secrets expire: renew it here.</small></label>
          <label className="field"><span>Send from (mailbox)</span>
            <input type="text" value={cfg.from_addr} placeholder="alerts@contoso.com" onChange={(e) => set("from_addr", e.target.value)} /></label>
        </div> : <div className="form cols">
          <label className="field"><span>{prov?.username_is_from ? "Email address (sends as and logs in as)" : "From address"}</span>
            <input type="text" value={cfg.from_addr} placeholder="alerts@example.com" onChange={(e) => set("from_addr", e.target.value)} /></label>
          {!prov?.username_is_from && <label className="field"><span>Username{cfg.provider === "custom" ? " (optional)" : ""}</span>
            <input type="text" value={cfg.username ?? ""} readOnly={userFixed} autoComplete="off"
                   onChange={(e) => set("username", e.target.value)} />
            {userFixed && <small>Fixed by {prov?.label}.</small>}</label>}
          <label className="field"><span>{prov?.secret_label ?? "Password"}{ch?.has_password ? " (leave empty to keep)" : ""}</span>
            <input type="password" value={pw} autoComplete="new-password" onChange={(e) => { setTested(null); setPw(e.target.value); }} />
            <small>Stored encrypted in the secret store; never shown again.</small></label>
        </div>}
        {!graph && <details open={cfg.provider === "custom" || !cfg.host}>
          <summary className="small">Server settings{cfg.host ? `: ${cfg.host}:${cfg.port}` : ""}</summary>
          <div className="form cols" style={{ marginTop: 8 }}>
            <label className="field"><span>SMTP server</span>
              <input type="text" value={cfg.host} placeholder="smtp.example.com" onChange={(e) => set("host", e.target.value)} /></label>
            <label className="field"><span>Port</span>
              <input type="number" min={1} max={65535} value={cfg.port} onChange={(e) => set("port", Number(e.target.value))} /></label>
            <label className="field"><span>Security</span>
              <select value={cfg.security} onChange={(e) => { set("security", e.target.value); set("port", e.target.value === "ssl" ? 465 : e.target.value === "none" ? 25 : 587); }}>
                <option value="starttls">STARTTLS (587)</option><option value="ssl">SSL/TLS (465)</option><option value="none">None (25, internal relay)</option></select></label>
            {prov?.username_is_from && <label className="field"><span>Login username</span>
              <input type="text" value={cfg.username ?? ""} placeholder={cfg.from_addr || "same as the email address"}
                     onChange={(e) => set("username", e.target.value)} /></label>}
          </div>
          <label className="check"><input type="checkbox" checked={cfg.verify_tls} onChange={(e) => set("verify_tls", e.target.checked)} /> Verify the server's TLS certificate</label>
        </details>}
        <label className="field"><span>Send alerts to</span>
          <input type="text" value={to} placeholder="ops@example.com, dr-team@example.com" onChange={(e) => { setTested(null); setTo(e.target.value); }} />
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
        <label className="check"><input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> Enabled</label>
        <label className="check"><input type="checkbox" checked={skip} onChange={(e) => setSkip(e.target.checked)} /> Save without checking the login first</label>
        {tested && <div className="alert ok small">{tested}</div>}
        <ErrorBox error={err} title={errFrom === "test" ? "Test email failed" : "Not saved"} />
      </div>
    </Modal>);
}

function Notifications({ onToast }: { onToast: (m: string) => void }) {
  const chans = useLoad(() => api.get<Channel[]>("/notification-channels"), [], 15000);
  const provs = useLoad(() => api.get<EmailProvider[]>("/notification-providers"), []);
  const provLabel = (c: Channel) => (provs.data ?? []).find((p) => p.id === (c.config.provider || "custom"))?.label;
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
              <div className="muted small">{provLabel(c) ?? "SMTP"}{c.config.provider !== "office365_graph" && c.config.host ? ` · ${c.config.host}:${c.config.port}` : ""}
                {" → "}{c.config.to.join(", ")}</div>
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
      {edit && provs.data && <ChannelEditor ch={edit === "new" ? null : edit} providers={provs.data} onClose={() => setEdit(null)}
                              onDone={(m) => { setEdit(null); chans.reload(); onToast(m); }} />}
    </div>);
}

export default function Settings() {
  const { can } = useAuth();
  const admin = can("admin");
  const users = useLoad(() => (admin ? api.get<User[]>("/users") : Promise.resolve([] as User[])), [admin]);
  const secrets = useLoad(() => (admin ? api.get<Record<string, unknown>>("/secrets/status") : Promise.resolve(null)), [admin]);
  const prov = useLoad(() => api.get<Providers>("/providers"));
  const [add, setAdd] = useState(false);
  const [editUser, setEditUser] = useState<User | null>(null);
  const pools = useLoad(() => api.get<Pool[]>("/pools"), [], 5000);
  const [addPool, setAddPool] = useState(false);
  const [join, setJoin] = useState<Pool | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  return (
    <>
      <div className="page-head"><div><h1>Settings</h1><div className="sub">Alert notifications, users, workers, secrets and platform support.</div></div></div>
      <div className="grid cols-2" style={{ alignItems: "start" }}>
        {admin && <Notifications onToast={setToast} />}
        <AccountCard onToast={setToast} />
        <div className="card">
          <div className="spread" style={{ marginBottom: 12 }}><h2 style={{ margin: 0 }}>Worker pools</h2>
            {admin && <button className="btn small" onClick={() => setAddPool(true)}>Add pool</button>}</div>
          <ErrorBox error={pools.error} />
          {(pools.data ?? []).length === 0 ? <p className="muted">No pools. Without pools any capable worker runs any job. A pool groups
            worker hosts (for example one per site or network): a migration or endpoint pinned to a pool runs only on its hosts, and a
            pool can start worker VMs on VHI/OpenStack by itself to keep a minimum number of hosts.</p> : (
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

      <WorkersCard admin={admin} onToast={setToast} />

      {admin && (
        <div className="card">
          <div className="spread" style={{ marginBottom: 12 }}><h2 style={{ margin: 0 }}>Users</h2>
            <button className="btn sm primary" onClick={() => setAdd(true)}>Add user</button></div>
          <ErrorBox error={users.error} />
          <div className="table-wrap"><table>
            <thead><tr><th>User</th><th>Role</th><th>Project</th><th>Created</th><th /></tr></thead>
            <tbody>{(users.data ?? []).map((u) => (
              <tr key={u.id}><td><b>{u.username}</b>{u.disabled && <span className="muted"> (disabled)</span>}</td>
                <td><span className="tag">{u.role}</span></td><td>{u.project}</td><td className="small muted">{fmt.time(u.created_at)}</td>
                <td style={{ textAlign: "right" }}><button className="btn sm" onClick={() => setEditUser(u)}>Edit</button></td></tr>))}</tbody>
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
      {editUser && <UserEdit user={editUser} onClose={() => setEditUser(null)}
                             onDone={(m) => { setEditUser(null); setToast(m); users.reload(); }} />}
      {addPool && <NewPool onClose={() => setAddPool(false)} onDone={(m) => { setAddPool(false); setToast(m); pools.reload(); }} />}
      {join && <JoinToken pool={join} onClose={() => setJoin(null)} />}
      <Toast msg={toast} onDone={() => setToast(null)} />
    </>
  );
}
