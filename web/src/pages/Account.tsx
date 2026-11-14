// Purpose: Settings -> Your account (change your own password) and the admin's per-user actions
// (set a user's password, change role, disable / enable). A forgotten admin password is reset on the
// server with scripts/reset-admin-password.sh.
// Usage: <AccountCard onToast={fn} />; <UserEdit user={u} onClose onDone />

import { useState } from "react";
import { api } from "../api";
import { ErrorBox, Modal, errText, useAuth } from "../ui";

export interface User { id: string; username: string; role: string; project: string; disabled: boolean; created_at: number }

function NewPassword({ pw, setPw, again, setAgain }: { pw: string; setPw: (s: string) => void; again: string; setAgain: (s: string) => void }) {
  return <>
    <label className="field"><span>New password</span>
      <input type="password" value={pw} autoComplete="new-password" onChange={(e) => setPw(e.target.value)} />
      <small>At least 12 characters.{pw && pw.length < 12 ? ` ${12 - pw.length} more.` : ""}</small></label>
    <label className="field"><span>Repeat new password</span>
      <input type="password" value={again} autoComplete="new-password" onChange={(e) => setAgain(e.target.value)} />
      {again && again !== pw && <small style={{ color: "var(--bad)" }}>Does not match.</small>}</label>
  </>;
}

function ChangePassword({ onClose, onDone }: { onClose: () => void; onDone: (m: string) => void }) {
  const [cur, setCur] = useState("");
  const [pw, setPw] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const ok = !!cur && pw.length >= 12 && pw === again && pw !== cur;
  return (
    <Modal title="Change your password" onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={!ok || busy} onClick={async () => {
        setBusy(true); setErr(null);
        try {
          const r = await api.post<{ other_sessions_signed_out: number }>("/auth/password", { current_password: cur, new_password: pw });
          onDone(`Password changed${r.other_sessions_signed_out ? `; ${r.other_sessions_signed_out} other session(s) signed out` : ""}`);
        } catch (e) { setErr(errText(e)); } finally { setBusy(false); }
      }}>{busy ? "Saving…" : "Change password"}</button></>}>
      <div className="form">
        <label className="field"><span>Current password</span>
          <input type="password" value={cur} autoComplete="current-password" autoFocus onChange={(e) => setCur(e.target.value)} /></label>
        <NewPassword pw={pw} setPw={setPw} again={again} setAgain={setAgain} />
        {pw && cur && pw === cur && <div className="alert warn small">The new password must differ from the current one.</div>}
        <p className="small muted" style={{ margin: 0 }}>Your other browser sessions are signed out; this one stays signed in.</p>
        <ErrorBox error={err} title="Password not changed" />
      </div>
    </Modal>);
}

export function AccountCard({ onToast }: { onToast: (m: string) => void }) {
  const { me } = useAuth();
  const [open, setOpen] = useState(false);
  return (
    <div className="card">
      <div className="spread" style={{ marginBottom: 12 }}><h2 style={{ margin: 0 }}>Your account</h2>
        <button className="btn sm" onClick={() => setOpen(true)}>Change password</button></div>
      <dl className="kv"><dt>User</dt><dd><b>{me?.username}</b></dd><dt>Role</dt><dd><span className="tag">{me?.role}</span></dd>
        <dt>Project</dt><dd>{me?.project}</dd></dl>
      <p className="muted small" style={{ marginBottom: 0 }}>Forgot the admin password? On the server run{" "}
        <code>sudo bash /opt/margarita/app/scripts/reset-admin-password.sh</code>.</p>
      {open && <ChangePassword onClose={() => setOpen(false)} onDone={(m) => { setOpen(false); onToast(m); }} />}
    </div>);
}

export function UserEdit({ user, onClose, onDone }: { user: User; onClose: () => void; onDone: (m: string) => void }) {
  const { me } = useAuth();
  const self = me?.username === user.username;
  const [role, setRole] = useState(user.role);
  const [disabled, setDisabled] = useState(user.disabled);
  const [pw, setPw] = useState("");
  const [again, setAgain] = useState("");
  const [signOut, setSignOut] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const pwOk = !pw || (pw.length >= 12 && pw === again);
  const changed = role !== user.role || disabled !== user.disabled || !!pw;
  const save = async () => {
    setErr(null);
    try {
      if (role !== user.role || disabled !== user.disabled) await api.patch(`/users/${user.id}`, { role, disabled });
      if (pw) await api.put(`/users/${user.id}/password`, { new_password: pw, sign_out: signOut });
      onDone(`Saved ${user.username}`);
    } catch (e) { setErr(errText(e)); }
  };
  return (
    <Modal title={`User ${user.username}`} onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={!changed || !pwOk} onClick={save}>Save</button></>}>
      <div className="form">
        <label className="field"><span>Role</span><select value={role} onChange={(e) => setRole(e.target.value)}>
          <option value="viewer">viewer: read only</option><option value="operator">operator: run migrations</option>
          <option value="admin">admin: everything + users</option></select></label>
        <label className="check"><input type="checkbox" checked={disabled} disabled={self} onChange={(e) => setDisabled(e.target.checked)} />
          <span>Disabled<small>{self ? "You cannot disable your own account." : "Cannot sign in; current sessions end."}</small></span></label>
        <h3 style={{ margin: "8px 0 0" }}>Set a new password</h3>
        <NewPassword pw={pw} setPw={setPw} again={again} setAgain={setAgain} />
        {pw && <label className="check"><input type="checkbox" checked={signOut} onChange={(e) => setSignOut(e.target.checked)} />
          <span>Sign {self ? "my other sessions" : "the user"} out everywhere</span></label>}
        <ErrorBox error={err} title="Not saved" />
      </div>
    </Modal>);
}
