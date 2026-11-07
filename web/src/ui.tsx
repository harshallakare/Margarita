// Purpose: shared UI building blocks — data-loading hook, status pills, progress bars, modal,
// error box, icons, the auth context, and the "type the name to confirm" execute dialog.
// Usage: import { useLoad, Status, Bar, Modal, useAuth } from "./ui";

import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError, type Me, type Role } from "./api";

// ------------------------------------------------------------------ auth context
export interface Auth { me: Me | null; can: (r: Role) => boolean; logout: () => void; }
export const AuthCtx = createContext<Auth>({ me: null, can: () => false, logout: () => {} });
export const useAuth = () => useContext(AuthCtx);
const ORDER: Role[] = ["viewer", "operator", "admin"];
export const roleAtLeast = (have: Role | undefined, need: Role) =>
  !!have && ORDER.indexOf(have) >= ORDER.indexOf(need);

// ------------------------------------------------------------------ data loading
export function useLoad<T>(fn: () => Promise<T>, deps: unknown[] = [], pollMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const reload = useCallback(async () => {
    try {
      const d = await fnRef.current();
      setData(d); setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    setLoading(true);
    reload();
    if (!pollMs) return;
    const t = setInterval(reload, pollMs);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, error, loading, reload, setData };
}

export function errText(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  return e instanceof Error ? e.message : String(e);
}

// ------------------------------------------------------------------ small components
const TONE: Record<string, string> = {
  completed: "ok", done: "ok", ok: "ok", verified: "ok",
  failed: "bad", canceled: "plain", running: "run", queued: "warn", pending: "plain",
};
export function Status({ s, label }: { s: string | null | undefined; label?: string }) {
  if (!s) return <span className="muted">—</span>;
  return <span className={`pill ${TONE[s] ?? ""}`}>{label ?? s.replace(/_/g, " ")}</span>;
}

export function Bar({ pct, tone, lg }: { pct: number; tone?: "ok" | "bad"; lg?: boolean }) {
  return (
    <div className={`bar ${tone ?? ""} ${lg ? "lg" : ""}`} role="progressbar" aria-valuenow={Math.round(pct)}
         aria-valuemin={0} aria-valuemax={100}>
      <i style={{ width: `${Math.max(0, Math.min(100, pct))}%` }} />
    </div>
  );
}

export function ErrorBox({ error, title }: { error: string | null; title?: string }) {
  if (!error) return null;
  return <div className="alert bad" role="alert"><b>{title ?? "Something went wrong"}</b><pre>{error}</pre></div>;
}

export function Loading() { return <div className="empty">Loading…</div>; }

export function Modal({ title, children, footer, onClose }: {
  title: string; children: ReactNode; footer?: ReactNode; onClose: () => void;
}) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  return (
    <div className="overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}>
        <header><h2>{title}</h2><button className="btn ghost sm" onClick={onClose} aria-label="Close">✕</button></header>
        <div className="body">{children}</div>
        {footer && <footer>{footer}</footer>}
      </div>
    </div>
  );
}

/** Real (non-dry-run) execution needs the operator to type the migration name. */
export function ConfirmExecute({ name, vm, disks, onConfirm, onClose }: {
  name: string; vm: string; disks: number | null; onConfirm: () => Promise<void>; onClose: () => void;
}) {
  return <ConfirmTyped title="Run migration for real" word={name} action="Start migration" onClose={onClose}
    onConfirm={onConfirm} body={<>This will snapshot <b>{vm}</b> on the source, create {disks ?? "its"} disks and a VM on
      the target, and copy every disk. The source VM is never modified or deleted.</>} />;
}

/** Generic "type X to confirm" dialog for anything that changes a platform. */
export function ConfirmTyped({ title, word, body, action, danger, onConfirm, onClose, children }: {
  title: string; word: string; body: ReactNode; action: string; danger?: boolean; children?: ReactNode;
  onConfirm: () => Promise<void>; onClose: () => void;
}) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  return (
    <Modal title={title} onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className={`btn ${danger === false ? "primary" : "danger solid"}`} disabled={typed !== word || busy} onClick={async () => {
        setBusy(true); setErr(null);
        try { await onConfirm(); } catch (e) { setErr(errText(e)); setBusy(false); }
      }}>{busy ? "Starting…" : action}</button>
    </>}>
      <div className="stack">
        <div className="alert warn">{body}</div>
        {children}
        <label className="field"><span>Type <code>{word}</code> to confirm</span>
          <input type="text" value={typed} onChange={(e) => setTyped(e.target.value)} autoFocus /></label>
        <ErrorBox error={err} title="Could not start" />
      </div>
    </Modal>
  );
}

export function Toast({ msg, tone = "ok", onDone }: { msg: string | null; tone?: string; onDone: () => void }) {
  useEffect(() => {
    if (!msg) return;
    const t = setTimeout(onDone, 4000);
    return () => clearTimeout(t);
  }, [msg, onDone]);
  if (!msg) return null;
  return <div className={`toast alert ${tone}`} role="status">{msg}</div>;
}

// ------------------------------------------------------------------ icons (inline, stroke)
const P: Record<string, string> = {
  migrate: "M4 7h11l-3-3M20 17H9l3 3",
  endpoint: "M4 6h16v5H4zM4 13h16v5H4zM8 8.5h.01M8 15.5h.01",
  replica: "M4 12a8 8 0 0 1 13.7-5.6L20 9M20 4v5h-5M20 12a8 8 0 0 1-13.7 5.6L4 15M4 20v-5h5",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  plus: "M12 5v14M5 12h14",
  check: "M5 12l5 5L20 7",
};
export function Icon({ n, size = 16 }: { n: keyof typeof P | string; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}
         strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={P[n] ?? ""} /></svg>
  );
}
