// Purpose: Endpoints screen — list, add/edit with type-specific forms (field schema comes from the
// adapter via GET /providers), validate (test connection), delete. Secret fields are write-only:
// on edit they start empty and are only sent when you type a new value.

import { useMemo, useState } from "react";
import { api, fmt, type Endpoint, type Field, type Providers } from "../api";
import { ErrorBox, Icon, Loading, Modal, Status, Toast, errText, useAuth, useLoad } from "../ui";

interface Validation { ok: boolean; roles: Record<string, { ok: boolean; problems: string[] }> }

function fieldsFor(p: Providers | null, type: string): Field[] {
  if (!p) return [];
  const all = [...p.sources, ...p.targets].filter((r) => r.type === type).flatMap((r) => r.fields);
  return all.filter((f, i) => all.findIndex((g) => g.name === f.name) === i);
}

export function roles(p: Providers | null, type: string): string[] {
  if (!p) return [];
  const r: string[] = [];
  if (p.sources.some((s) => s.type === type && s.status === "implemented")) r.push("source");
  if (p.targets.some((s) => s.type === type && s.status === "implemented")) r.push("target");
  return r;
}

function EndpointForm({ providers, existing, onClose, onSaved }: {
  providers: Providers; existing: Endpoint | null; onClose: () => void; onSaved: (msg: string) => void;
}) {
  const types = useMemo(() => {
    const t = new Map<string, string>();
    [...providers.sources, ...providers.targets].forEach((r) => {
      if (r.status === "implemented" && !t.has(r.type)) t.set(r.type, r.notes);
    });
    return [...t.entries()];
  }, [providers]);
  const [type, setType] = useState(existing?.type ?? "");
  const [name, setName] = useState(existing?.name ?? "");
  const [desc, setDesc] = useState(existing?.description ?? "");
  const [region, setRegion] = useState(existing?.region ?? "");
  const [tags, setTags] = useState((existing?.required_tags ?? []).join(", "));
  const fields = fieldsFor(providers, type);
  const known = new Set(fields.map((f) => f.name));
  const init = (): Record<string, string | boolean> => {
    const v: Record<string, string | boolean> = {};
    for (const f of fields) {
      const cur = existing?.config[f.name] ?? (existing ? undefined : f.default);
      if (f.secret) v[f.name] = "";
      else if (f.kind === "bool") v[f.name] = cur === undefined ? false : Boolean(cur);
      else if (f.kind === "json") v[f.name] = cur === undefined ? "" : JSON.stringify(cur, null, 2);
      else v[f.name] = cur === undefined || cur === null ? "" : String(cur);
    }
    return v;
  };
  const [vals, setVals] = useState<Record<string, string | boolean>>(init);
  const [extra, setExtra] = useState(() => {
    const rest = Object.fromEntries(Object.entries(existing?.config ?? {}).filter(([k]) => !known.has(k)));
    return Object.keys(rest).length ? JSON.stringify(rest, null, 2) : "";
  });
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const pick = (t: string) => {
    setType(t);
    const v: Record<string, string | boolean> = {};
    for (const f of fieldsFor(providers, t)) {
      v[f.name] = f.kind === "bool" ? Boolean(f.default) : f.default == null || f.secret ? "" :
        f.kind === "json" ? JSON.stringify(f.default) : String(f.default);
    }
    setVals(v);
  };

  const submit = async () => {
    setErr(null);
    const config: Record<string, unknown> = {};
    const creds: Record<string, unknown> = {};
    try {
      for (const f of fields) {
        const v = vals[f.name];
        if (f.secret) {
          if (v) creds[f.name] = v;
          else if (f.required && !existing?.credential_keys.includes(f.name)) throw new Error(`${f.label} is required`);
          continue;
        }
        if (f.kind === "bool") { config[f.name] = Boolean(v); continue; }
        if (v === "" || v === undefined) {
          if (f.required) throw new Error(`${f.label} is required`);
          continue;
        }
        if (f.kind === "int") {
          const n = Number(v);
          if (!Number.isInteger(n)) throw new Error(`${f.label} must be a whole number`);
          config[f.name] = n;
        } else if (f.kind === "json") {
          try { config[f.name] = JSON.parse(String(v)); } catch { throw new Error(`${f.label} is not valid JSON`); }
        } else config[f.name] = String(v).trim();
      }
      if (extra.trim()) {
        let x: unknown;
        try { x = JSON.parse(extra); } catch { throw new Error("Advanced config is not valid JSON"); }
        if (typeof x !== "object" || x === null || Array.isArray(x)) throw new Error("Advanced config must be a JSON object");
        Object.assign(config, x);
      }
    } catch (e) { setErr(errText(e)); return; }
    const tagList = tags.split(",").map((t) => t.trim()).filter(Boolean);
    setBusy(true);
    try {
      if (existing) {
        await api.patch(`/endpoints/${existing.id}`, {
          description: desc, config, region, required_tags: tagList,
          ...(Object.keys(creds).length ? { credentials: creds } : {}),
        });
        onSaved(`Saved ${existing.name}`);
      } else {
        await api.post("/endpoints", { name, type, description: desc, config, credentials: creds,
          region: region || null, required_tags: tagList });
        onSaved(`Added ${name}`);
      }
    } catch (e) { setErr(errText(e)); setBusy(false); }
  };

  return (
    <Modal title={existing ? `Edit ${existing.name}` : "Add endpoint"} onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={busy || !type || !name} onClick={submit}>
        {busy ? "Validating connection…" : existing ? "Validate & save" : "Validate & add"}</button>
    </>}>
      <div className="stack">
        {!existing && (
          <div className="field"><span>Platform</span>
            <div className="choices">
              {types.map(([t, notes]) => (
                <button key={t} type="button" className={`choice ${type === t ? "on" : ""}`} onClick={() => pick(t)}>
                  <div className="t">{t}</div><div className="d">{roles(providers, t).join(" + ")} · {notes}</div>
                </button>
              ))}
            </div>
          </div>
        )}
        {type && (
          <div className="form cols">
            <label className="field"><span>Name<span className="req">*</span></span>
              <input type="text" value={name} disabled={!!existing} onChange={(e) => setName(e.target.value)}
                     placeholder="ahv-prod" /></label>
            <label className="field"><span>Description</span>
              <input type="text" value={desc} onChange={(e) => setDesc(e.target.value)} /></label>
            {fields.map((f) => f.kind === "bool" ? (
              <label key={f.name} className="check wide">
                <input type="checkbox" checked={Boolean(vals[f.name])}
                       onChange={(e) => setVals({ ...vals, [f.name]: e.target.checked })} />
                <span>{f.label}{f.help && <small>{f.help}</small>}</span>
              </label>
            ) : (
              <label key={f.name} className={`field ${f.kind === "json" || f.kind === "textarea" ? "wide" : ""}`}>
                <span>{f.label}{f.required && <span className="req">*</span>}
                  {f.secret && <span className="tag" style={{ marginLeft: 8 }}>encrypted</span>}</span>
                {f.kind === "json" || f.kind === "textarea" ? (
                  <textarea value={String(vals[f.name] ?? "")} spellCheck={false} autoComplete="off"
                            placeholder={f.secret && existing?.credential_keys.includes(f.name) ? "•••••• (unchanged)" : ""}
                            onChange={(e) => setVals({ ...vals, [f.name]: e.target.value })} />
                ) : f.kind === "select" ? (
                  <select value={String(vals[f.name] ?? "")} onChange={(e) => setVals({ ...vals, [f.name]: e.target.value })}>
                    {(f.options ?? []).map((o) => <option key={o} value={o}>{o}</option>)}
                  </select>
                ) : (
                  <input type={f.secret ? "password" : f.kind === "int" ? "number" : f.kind === "url" ? "url" : "text"}
                         value={String(vals[f.name] ?? "")} autoComplete={f.secret ? "new-password" : "off"}
                         placeholder={f.secret && existing?.credential_keys.includes(f.name) ? "•••••• (unchanged)" : ""}
                         onChange={(e) => setVals({ ...vals, [f.name]: e.target.value })} />
                )}
                {f.help && <small>{f.help}</small>}
              </label>
            ))}
            <label className="field"><span>Region / site (optional)</span>
              <input type="text" value={region} placeholder="e.g. dc-pune" onChange={(e) => setRegion(e.target.value)} />
              <small>Jobs for this endpoint run only on workers in the same region.</small></label>
            <label className="field"><span>Required worker tags (optional)</span>
              <input type="text" value={tags} placeholder="e.g. vstorage, site-a" onChange={(e) => setTags(e.target.value)} />
              <small>Comma-separated capabilities a worker must have to use this endpoint.</small></label>
            <label className="field wide"><span>Advanced config (JSON)</span>
              <textarea value={extra} onChange={(e) => setExtra(e.target.value)} placeholder='{"poll_interval": 10}' />
              <small>Extra non-secret adapter keys. Password-like keys are rejected here; use the encrypted fields.</small>
            </label>
          </div>
        )}
        {busy && <div className="alert info">Checking that the platform is reachable and the credentials work. The
          endpoint is saved only if this succeeds.</div>}
        <ErrorBox error={err} title="Not saved" />
      </div>
    </Modal>
  );
}

export default function Endpoints() {
  const { can } = useAuth();
  const eps = useLoad(() => api.get<Endpoint[]>("/endpoints"));
  const prov = useLoad(() => api.get<Providers>("/providers"));
  const [edit, setEdit] = useState<Endpoint | null | "new">(null);
  const [checks, setChecks] = useState<Record<string, Validation | "busy" | string>>({});
  const [toast, setToast] = useState<string | null>(null);
  const [del, setDel] = useState<Endpoint | null>(null);
  const [delErr, setDelErr] = useState<string | null>(null);

  const validate = async (e: Endpoint) => {
    setChecks((c) => ({ ...c, [e.id]: "busy" }));
    try {
      const v = await api.post<Validation>(`/endpoints/${e.id}/validate`);
      setChecks((c) => ({ ...c, [e.id]: v }));
    } catch (ex) { setChecks((c) => ({ ...c, [e.id]: errText(ex) })); }
  };

  return (
    <>
      <div className="page-head">
        <div><h1>Endpoints</h1><div className="sub">Named, validated connections to source and target platforms.</div></div>
        {can("operator") && <button className="btn primary" onClick={() => setEdit("new")}><Icon n="plus" />Add endpoint</button>}
      </div>
      <ErrorBox error={eps.error || prov.error} />
      {eps.loading && !eps.data ? <Loading /> : eps.data?.length === 0 ? (
        <div className="card empty"><h2>No endpoints yet</h2>
          <p>Add the source platform you are migrating from and the target you are migrating to.</p>
          {can("operator") && <button className="btn primary" onClick={() => setEdit("new")}>Add endpoint</button>}</div>
      ) : (
        <div className="card"><div className="table-wrap"><table>
          <thead><tr><th>Name</th><th>Platform</th><th>Can be</th><th>Credentials</th><th>Connection</th><th>Updated</th><th /></tr></thead>
          <tbody>{eps.data?.map((e) => {
            const c = checks[e.id];
            return (
              <tr key={e.id}>
                <td><b className="name">{e.name}</b>{e.description && <div className="muted small" style={{ minWidth: 160 }}>{e.description}</div>}</td>
                <td><span className="tag">{e.type}</span></td>
                <td className="small">{roles(prov.data, e.type).join(" / ") || "—"}</td>
                <td className="small">{e.has_credentials ? <span title={e.credential_keys.join(", ")}>🔒 {e.credential_keys.length} stored</span> : <span className="muted">none</span>}</td>
                <td style={{ maxWidth: 360 }}>
                  {c === undefined ? <span className="muted small">not checked</span>
                    : c === "busy" ? <span className="pill run">checking</span>
                    : typeof c === "string" ? <span className="pill bad" title={c}>error</span>
                    : c.ok ? <Status s="ok" label="reachable" />
                    : <div><span className="pill bad">problems</span>
                        <ul className="small" style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                          {Object.entries(c.roles).flatMap(([r, v]) => v.problems.map((p, i) => <li key={r + i}>{r}: {p}</li>))}
                        </ul></div>}
                </td>
                <td className="small muted">{fmt.ago(e.updated_at)}</td>
                <td className="actions"><div className="row">
                  <button className="btn sm" onClick={() => validate(e)}>Test</button>
                  {can("operator") && <button className="btn sm" onClick={() => setEdit(e)}>Edit</button>}
                  {can("operator") && <button className="btn sm danger" onClick={() => { setDelErr(null); setDel(e); }}>Delete</button>}
                </div></td>
              </tr>
            );
          })}</tbody>
        </table></div></div>
      )}
      {edit && prov.data && (
        <EndpointForm providers={prov.data} existing={edit === "new" ? null : edit} onClose={() => setEdit(null)}
                      onSaved={(m) => { setEdit(null); setToast(m); eps.reload(); }} />
      )}
      {del && (
        <Modal title={`Delete ${del.name}?`} onClose={() => setDel(null)} footer={<>
          <button className="btn" onClick={() => setDel(null)}>Cancel</button>
          <button className="btn danger solid" onClick={async () => {
            try { await api.del(`/endpoints/${del.id}`); setDel(null); setToast(`Deleted ${del.name}`); eps.reload(); }
            catch (e) { setDelErr(errText(e)); }
          }}>Delete endpoint</button></>}>
          <p style={{ marginTop: 0 }}>Removes the connection and its stored credentials from vmigrate. Nothing on the platform itself is touched.</p>
          <ErrorBox error={delErr} title="Could not delete" />
        </Modal>
      )}
      <Toast msg={toast} onDone={() => setToast(null)} />
    </>
  );
}
