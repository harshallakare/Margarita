// Purpose: SPA entry — auth bootstrap (token -> /auth/me), the sidebar shell, and the routes.
// Usage: built by Vite; routes live under /ui (BrowserRouter basename).

import { StrictMode, useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, NavLink, Route, Routes } from "react-router-dom";
import { api, setUnauthorizedHandler, token, type Me } from "./api";
import { AuthCtx, ErrorBox, Icon, roleAtLeast } from "./ui";
import Audit from "./pages/Audit";
import Endpoints from "./pages/Endpoints";
import Execution from "./pages/Execution";
import Logs from "./pages/Logs";
import MigrationDetail from "./pages/MigrationDetail";
import Migrations from "./pages/Migrations";
import ReplicaDetail from "./pages/ReplicaDetail";
import Replicas from "./pages/Replicas";
import Settings from "./pages/Settings";
import Wizard from "./pages/Wizard";
import "./styles.css";

function Login({ onLogin }: { onLogin: (m: Me) => void }) {
  const [u, setU] = useState("");
  const [p, setP] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <div className="login">
      <form className="card form" onSubmit={async (e) => {
        e.preventDefault(); setBusy(true); setErr(null);
        try {
          const r = await api.post<{ token: string }>("/auth/token", { username: u, password: p });
          token.set(r.token);
          onLogin(await api.get<Me>("/auth/me"));
        } catch (ex) {
          setErr(ex instanceof Error ? ex.message : String(ex));
        } finally { setBusy(false); }
      }}>
        <div className="brand"><span className="brand-mark"><Icon n="migrate" /></span>vmigrate</div>
        <label className="field"><span>Username</span>
          <input type="text" value={u} onChange={(e) => setU(e.target.value)} autoComplete="username" autoFocus /></label>
        <label className="field"><span>Password</span>
          <input type="password" value={p} onChange={(e) => setP(e.target.value)} autoComplete="current-password" /></label>
        <ErrorBox error={err} title="Sign-in failed" />
        <button className="btn primary" disabled={busy || !u || !p}>{busy ? "Signing in…" : "Sign in"}</button>
        <p className="muted small" style={{ margin: 0, textAlign: "center" }}>
          Create users with <code>vmigrate-platform create-user</code>.
        </p>
      </form>
    </div>
  );
}

function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [ready, setReady] = useState(false);
  const logout = useCallback(() => {
    api.post("/auth/logout").catch(() => {});
    token.set(null); setMe(null);
  }, []);
  useEffect(() => {
    setUnauthorizedHandler(() => setMe(null));
    if (!token.get()) { setReady(true); return; }
    api.get<Me>("/auth/me").then(setMe).catch(() => token.set(null)).finally(() => setReady(true));
  }, []);
  if (!ready) return null;
  if (!me) return <Login onLogin={setMe} />;
  const can = (r: Me["role"]) => roleAtLeast(me.role, r);
  return (
    <AuthCtx.Provider value={{ me, can, logout }}>
      <div className="shell">
        <aside className="side">
          <div className="brand"><span className="brand-mark"><Icon n="migrate" /></span>vmigrate</div>
          <nav className="nav">
            <NavLink to="/migrations"><Icon n="migrate" />Migrations</NavLink>
            <NavLink to="/endpoints"><Icon n="endpoint" />Endpoints</NavLink>
            <NavLink to="/replicas"><Icon n="replica" />Replicas</NavLink>
            <NavLink to="/logs"><Icon n="logs" />Logs</NavLink>
            {can("admin") && <NavLink to="/audit"><Icon n="audit" />Audit trail</NavLink>}
            <NavLink to="/settings"><Icon n="settings" />Settings</NavLink>
          </nav>
          <div className="side-foot">
            <div className="spread"><span><b>{me.username}</b> · {me.role}</span>
              <button className="btn ghost sm" onClick={logout}>Sign out</button></div>
            <div className="muted">project {me.project}</div>
          </div>
        </aside>
        <main className="main">
          <Routes>
            <Route path="/" element={<Navigate to="/migrations" replace />} />
            <Route path="/migrations" element={<Migrations />} />
            <Route path="/migrations/new" element={<Wizard />} />
            <Route path="/migrations/:id" element={<MigrationDetail />} />
            <Route path="/replicas" element={<Replicas />} />
            <Route path="/replicas/:id" element={<ReplicaDetail />} />
            <Route path="/executions/:id" element={<Execution />} />
            <Route path="/endpoints" element={<Endpoints />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/logs" element={<Logs />} />
            <Route path="/audit" element={<Audit />} />
            <Route path="*" element={<div className="empty"><h2>Not found</h2></div>} />
          </Routes>
        </main>
      </div>
    </AuthCtx.Provider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode><BrowserRouter basename="/ui"><App /></BrowserRouter></StrictMode>,
);
