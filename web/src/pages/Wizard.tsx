// Purpose: the New Migration wizard. source endpoint -> VM(s) from inventory (each one inspected:
// TRUE disk count, sizes, and the disk-count gate, which blocks the wizard when enumerations
// disagree) -> target endpoint -> network/storage/flavor mapping -> options -> review -> create.
// Creating starts a DRY-RUN; a real run is launched from the migration page after reviewing it.

import { Fragment, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, fmt, type Endpoint, type Execution, type Inspect, type Migration, type Providers, type Schedule } from "../api";
import { ErrorBox, Icon, Loading, Status, errText, useAuth, useLoad } from "../ui";
import { roles } from "./Endpoints";

const STEPS = ["Source", "Virtual machines", "Target", "Mapping", "Options", "Review"];
const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 100) || "vm";

interface Inv { id: string; name: string }
type InspectState = Inspect | "loading" | { error: string };

function Gate({ i }: { i: InspectState | undefined }) {
  if (!i || i === "loading") return <span className="pill run">inspecting</span>;
  if ("error" in i) return <span className="pill bad" title={i.error}>inspect failed</span>;
  return i.disk_gate.ok ? <Status s="ok" label={`${i.vm.disks.length} disks · counts agree`} />
    : <span className="pill bad">disk counts disagree</span>;
}

export default function Wizard() {
  const nav = useNavigate();
  const [params, setParams] = useSearchParams();
  const replica = params.get("mode") === "replica";
  const [retention, setRetention] = useState(3);
  const [cron, setCron] = useState("");
  const [cronOk, setCronOk] = useState<{ valid: boolean; error?: string; next: number[] } | null>(null);
  useEffect(() => {
    if (!cron.trim()) { setCronOk(null); return; }
    const t = setTimeout(() => api.get<{ valid: boolean; error?: string; next: number[] }>(
      `/schedules/preview?cron=${encodeURIComponent(cron)}`).then(setCronOk).catch(() => setCronOk(null)), 250);
    return () => clearTimeout(t);
  }, [cron]);
  const { can } = useAuth();
  const eps = useLoad(() => api.get<Endpoint[]>("/endpoints"));
  const prov = useLoad(() => api.get<Providers>("/providers"));
  const [step, setStep] = useState(0);
  const [src, setSrc] = useState<Endpoint | null>(null);
  const [tgt, setTgt] = useState<Endpoint | null>(null);
  const [inv, setInv] = useState<Inv[] | null>(null);
  const [invErr, setInvErr] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [sel, setSel] = useState<string[]>([]);
  const [insp, setInsp] = useState<Record<string, InspectState>>({});
  const [open, setOpen] = useState<string | null>(null);
  const [netMap, setNetMap] = useState<Record<string, string>>({});
  const [storagePool, setStoragePool] = useState("");
  const [flavor, setFlavor] = useState("");
  const [az, setAz] = useState("");
  const [prefix, setPrefix] = useState("");
  const [targetName, setTargetName] = useState("");
  const [appConsistent, setAppConsistent] = useState(true);
  const [parallel, setParallel] = useState(4);
  const [onFailure, setOnFailure] = useState<"keep" | "cleanup">("keep");
  const [bootCheck, setBootCheck] = useState(true);
  const [fstabHarden, setFstabHarden] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const sources = (eps.data ?? []).filter((e) => roles(prov.data, e.type).includes("source"));
  const targets = (eps.data ?? []).filter((e) => roles(prov.data, e.type).includes("target"));

  useEffect(() => {
    if (!src) return;
    setInv(null); setInvErr(null); setSel([]); setInsp({});
    api.get<Inv[]>(`/endpoints/${src.id}/instances`).then(setInv).catch((e) => setInvErr(errText(e)));
  }, [src]);

  const toggle = (vm: string) => {
    if (sel.includes(vm)) { setSel(sel.filter((v) => v !== vm)); return; }
    setSel([...sel, vm]);
    if (!insp[vm] && src) {
      setInsp((s) => ({ ...s, [vm]: "loading" }));
      api.get<Inspect>(`/endpoints/${src.id}/instances/${encodeURIComponent(vm)}`)
        .then((r) => setInsp((s) => ({ ...s, [vm]: r })))
        .catch((e) => setInsp((s) => ({ ...s, [vm]: { error: errText(e) } })));
    }
  };

  const chosen = sel.map((v) => insp[v]).filter((i): i is Inspect => !!i && i !== "loading" && !("error" in i));
  const allInspected = sel.length > 0 && chosen.length === sel.length;
  const gateOk = allInspected && chosen.every((i) => i.disk_gate.ok);
  const networks = useMemo(() => [...new Set(chosen.flatMap((i) => i.vm.nics.map((n) => n.network)))], [chosen]);
  const totalDisks = chosen.reduce((a, i) => a + i.vm.disks.length, 0);
  const totalBytes = chosen.reduce((a, i) => a + i.vm.disks.reduce((b, d) => b + d.size_bytes, 0), 0);
  const filtered = (inv ?? []).filter((v) => v.name.toLowerCase().includes(q.toLowerCase()));

  useEffect(() => { if (sel.length === 1 && !prefix) setPrefix(slug(sel[0])); }, [sel, prefix]);

  const canNext = [!!src, gateOk, !!tgt, true,
    parallel >= 1 && parallel <= 64 && (!replica || ((retention >= 1 && retention <= 100) && (!cron.trim() || !!cronOk?.valid))),
    !!prefix][step];

  const names = sel.map((vm) => (sel.length === 1 ? prefix : `${prefix}-${slug(vm)}`));

  const create = async () => {
    setBusy(true); setErr(null);
    const made: { mig: Migration; exec: Execution }[] = [];
    try {
      for (const [i, vm] of sel.entries()) {
        const target_options: Record<string, unknown> = {};
        const nm = Object.fromEntries(Object.entries(netMap).filter(([, v]) => v.trim()));
        if (Object.keys(nm).length) target_options.network_map = nm;
        if (storagePool.trim()) target_options.storage_pool = storagePool.trim();
        if (flavor.trim()) target_options.flavor = flavor.trim();
        if (az.trim()) target_options.availability_zone = az.trim();
        const options = {
          ...(sel.length === 1 && targetName.trim() ? { target_name: targetName.trim() } : {}),
          app_consistent: appConsistent, target_options,
          osmorph: { fstab_harden: fstabHarden, disable_periodic_fsck: fstabHarden },
          engine: { max_parallel_disks: parallel, on_failure: onFailure, boot_check: bootCheck },
          ...(replica ? { retention } : {}),
        };
        const body = { name: names[i], source_endpoint_id: src!.id, target_endpoint_id: tgt!.id, vm, options };
        if (replica) {
          const rep = await api.post<Migration & { schedules: Schedule[] }>("/replicas", { ...body, schedule: cron.trim() || null });
          const exec = await api.post<Execution>(`/replicas/${rep.id}/execute`, { dry_run: true });
          made.push({ mig: rep, exec });
        } else {
          const mig = await api.post<Migration>("/migrations", body);
          const exec = await api.post<Execution>(`/migrations/${mig.id}/start`, { dry_run: true });
          made.push({ mig, exec });
        }
      }
      const base = replica ? "/replicas" : "/migrations";
      nav(made.length === 1 ? `${base}/${made[0].mig.id}` : base);
    } catch (e) {
      setErr(errText(e) + (made.length ? `\n(${made.length} migration(s) were created before this error)` : ""));
      setBusy(false);
    }
  };

  if (!can("operator")) return <div className="card empty"><h2>Operators only</h2><p>Your role can view migrations but not create them.</p></div>;
  if (eps.loading || prov.loading) return <Loading />;

  return (
    <>
      <div className="page-head"><div>
        <div className="crumbs"><Link to={replica ? "/replicas" : "/migrations"}>{replica ? "Replicas" : "Migrations"}</Link> /</div>
        <h1>{replica ? "New replica" : "New migration"}</h1>
        <div className="sub">{replica ? "Initial full copy, then incremental syncs on demand or on a schedule. Deploy a VM from it at any time."
          : "One-shot copy → OSMorph → VM on the target. Starts with a dry-run plan."}</div>
      </div>
      {step === 0 && <div className="row">
        <button className={`btn sm ${!replica ? "primary" : ""}`} onClick={() => setParams({})}>Migration</button>
        <button className={`btn sm ${replica ? "primary" : ""}`} onClick={() => setParams({ mode: "replica" })}>Replica (DR)</button>
      </div>}</div>
      <div className="steps">{STEPS.map((s, i) => (
        <div key={s} className={`s ${i === step ? "on" : ""} ${i < step ? "done" : ""}`}>
          <i>{i < step ? <Icon n="check" size={12} /> : i + 1}</i>{s}</div>
      ))}</div>
      <ErrorBox error={eps.error || prov.error} />

      <div className="card">
        {step === 0 && (<>
          <h2>Where are the VMs now?</h2>
          {sources.length === 0 ? <div className="empty">No source-capable endpoints. <Link to="/endpoints">Add one</Link>.</div> :
            <div className="choices">{sources.map((e) => (
              <button key={e.id} className={`choice ${src?.id === e.id ? "on" : ""}`} onClick={() => setSrc(e)}>
                <div className="t">{e.name}</div><div className="d">{e.type}{e.description ? ` · ${e.description}` : ""}</div>
              </button>))}</div>}
        </>)}

        {step === 1 && (<>
          <div className="spread" style={{ marginBottom: 12 }}>
            <h2 style={{ margin: 0 }}>Pick VMs from {src?.name}</h2>
            <input type="text" placeholder="Filter by name" value={q} onChange={(e) => setQ(e.target.value)} style={{ maxWidth: 260 }} />
          </div>
          <ErrorBox error={invErr} title="Could not list VMs" />
          {!inv && !invErr ? <Loading /> : (
            <div className="table-wrap" style={{ marginBottom: 0 }}><table>
              <thead><tr><th style={{ width: 32 }} /><th>VM</th><th>Disks (true count)</th><th className="num">Total size</th><th>Disk-count gate</th><th /></tr></thead>
              <tbody>{filtered.map((v) => {
                const i = insp[v.name];
                const ok = i && i !== "loading" && !("error" in i) ? i : null;
                return (<Fragment key={v.id}>
                  <tr className={ok && !ok.disk_gate.ok ? "hl" : ""}>
                    <td><input type="checkbox" checked={sel.includes(v.name)} onChange={() => toggle(v.name)}
                               aria-label={`Select ${v.name}`} /></td>
                    <td><b>{v.name}</b><div className="muted small mono">{v.id}</div></td>
                    <td>{ok ? <>{ok.vm.disks.length}{ok.disk_gate.legacy_cap_exceeded &&
                      <span className="tag" style={{ marginLeft: 8 }} title="Coriolis's Nutanix provider would only see the first 10">≥ 10 — all enumerated</span>}</> : sel.includes(v.name) ? "…" : <span className="muted">select to inspect</span>}</td>
                    <td className="num">{ok ? fmt.bytes(ok.vm.disks.reduce((a, d) => a + d.size_bytes, 0)) : ""}</td>
                    <td>{sel.includes(v.name) ? <Gate i={i} /> : null}</td>
                    <td>{ok && <button className="btn sm ghost" onClick={() => setOpen(open === v.name ? null : v.name)}>
                      {open === v.name ? "Hide disks" : "Disks"}</button>}</td>
                  </tr>
                  {i && i !== "loading" && "error" in i && <tr key={v.id + "e"}><td /><td colSpan={5}><ErrorBox error={i.error} title="Inspect failed" /></td></tr>}
                  {ok && !ok.disk_gate.ok && <tr key={v.id + "g"}><td /><td colSpan={5}>
                    <div className="alert bad"><b>Refusing to migrate {v.name}: disk enumerations disagree.</b>
                      <pre>{Object.entries(ok.disk_gate.counts).map(([k, n]) => `${k}: ${n}`).join("\n")}</pre>
                      <div className="small" style={{ marginTop: 6 }}>Some disks would be silently dropped (the bug this tool exists to prevent). Fix the source-side enumeration first.</div></div></td></tr>}
                  {ok && open === v.name && <tr key={v.id + "d"}><td /><td colSpan={5}>
                    <div className="kv small" style={{ marginBottom: 8 }}>
                      <dt>CPU / RAM</dt><dd>{ok.vm.cpu.sockets * ok.vm.cpu.cores} vCPU · {fmt.bytes(ok.vm.memory_bytes)}</dd>
                      <dt>Firmware / OS</dt><dd>{ok.vm.firmware} · {ok.vm.guest_os.family}{ok.vm.guest_os.distro ? ` ${ok.vm.guest_os.distro}` : ""}</dd>
                      <dt>Counts</dt><dd className="mono">{Object.entries(ok.disk_gate.counts).map(([k, n]) => `${k}=${n}`).join("  ")}</dd>
                    </div>
                    <table><thead><tr><th>#</th><th>Bus</th><th className="num">Size</th><th>Boot</th></tr></thead>
                      <tbody>{[...ok.vm.disks].sort((a, b) => a.order - b.order).map((d) => (
                        <tr key={d.id} className={d.order >= 10 ? "hl" : ""}><td className="mono">{d.order}</td>
                          <td className="mono">{d.bus}.{d.bus_index ?? d.order}{d.order >= 10 && <span className="muted"> · beyond Coriolis's cap</span>}</td>
                          <td className="num">{fmt.bytes(d.size_bytes)}</td><td>{d.is_boot ? "boot" : ""}</td></tr>))}</tbody></table>
                  </td></tr>}
                </Fragment>);
              })}</tbody>
            </table></div>
          )}
          {sel.length > 0 && <p className="muted small" style={{ marginBottom: 0 }}>
            {sel.length} selected · {totalDisks} disks · {fmt.bytes(totalBytes)}. One migration is created per VM.</p>}
        </>)}

        {step === 2 && (<>
          <h2>Where should they go?</h2>
          {targets.length === 0 ? <div className="empty">No target-capable endpoints. <Link to="/endpoints">Add one</Link>.</div> :
            <div className="choices">{targets.filter((e) => e.id !== src?.id).map((e) => (
              <button key={e.id} className={`choice ${tgt?.id === e.id ? "on" : ""}`} onClick={() => setTgt(e)}>
                <div className="t">{e.name}</div><div className="d">{e.type}{e.description ? ` · ${e.description}` : ""}</div>
              </button>))}</div>}
        </>)}

        {step === 3 && (<div className="stack">
          <div><h2>Network mapping</h2>
            {networks.length === 0 ? <p className="muted">These VMs have no NICs.</p> : (
              <div className="table-wrap" style={{ margin: 0 }}><table>
                <thead><tr><th>Source network</th><th>Target network (name or ID)</th></tr></thead>
                <tbody>{networks.map((n) => (<tr key={n}><td className="mono">{n}</td><td>
                  <input type="text" value={netMap[n] ?? ""} placeholder="leave empty to use the target default"
                         onChange={(e) => setNetMap({ ...netMap, [n]: e.target.value })} /></td></tr>))}</tbody>
              </table></div>)}
          </div>
          <div><h2>Storage and sizing</h2>
            <div className="form cols">
              <label className="field"><span>Storage pool / volume type</span>
                <input type="text" value={storagePool} onChange={(e) => setStoragePool(e.target.value)} placeholder="target default" /></label>
              <label className="field"><span>Flavor</span>
                <input type="text" value={flavor} onChange={(e) => setFlavor(e.target.value)} placeholder="match source CPU/RAM" /></label>
              <label className="field"><span>Availability zone</span>
                <input type="text" value={az} onChange={(e) => setAz(e.target.value)} placeholder="target default" /></label>
              {sel.length === 1 && <label className="field"><span>Name on target</span>
                <input type="text" value={targetName} onChange={(e) => setTargetName(e.target.value)} placeholder={sel[0]} /></label>}
            </div>
          </div>
        </div>)}

        {step === 4 && (<div className="form">
          <h2 style={{ margin: 0 }}>Options</h2>
          <label className="check"><input type="checkbox" checked={appConsistent} onChange={(e) => setAppConsistent(e.target.checked)} />
            <span>Application-consistent snapshot<small>Quiesce the guest (NGT / VSS) before the snapshot that disks are read from.</small></span></label>
          <label className="check"><input type="checkbox" checked={fstabHarden} onChange={(e) => setFstabHarden(e.target.checked)} />
            <span>Harden Linux boot (fstab nofail + pass 0, no periodic fsck)<small>Avoids hours-long fsck or emergency mode on large data disks.</small></span></label>
          <label className="check"><input type="checkbox" checked={bootCheck} onChange={(e) => setBootCheck(e.target.checked)} />
            <span>Boot check after deploy<small>Watch the serial console for a login prompt.</small></span></label>
          <div className="form cols">
            <label className="field"><span>Disks copied in parallel</span>
              <input type="number" min={1} max={64} value={parallel} onChange={(e) => setParallel(Number(e.target.value))} />
              <small>Single NFS streams top out near 150 MB/s, so copying several disks at once is faster. Make sure target staging can hold this many of the largest disks.</small></label>
            {replica && <label className="field"><span>Restore points to keep</span>
              <input type="number" min={1} max={100} value={retention} onChange={(e) => setRetention(Number(e.target.value))} />
              <small>On targets that support them. Older ones are deleted after each sync.</small></label>}
            {replica && <label className="field"><span>Sync schedule (cron, UTC, optional)</span>
              <input type="text" className="mono" value={cron} placeholder="0 */4 * * *  (empty = manual)" onChange={(e) => setCron(e.target.value)} />
              <small>{!cron.trim() ? "Leave empty to sync manually." : cronOk?.valid ? `Next: ${cronOk.next.slice(0, 2).map((t) => fmt.time(t)).join(", ")}` : cronOk?.error ?? "checking…"}</small></label>}
            <label className="field"><span>If it fails</span>
              <select value={onFailure} onChange={(e) => setOnFailure(e.target.value as "keep" | "cleanup")}>
                <option value="keep">Keep what was created (retry resumes)</option>
                <option value="cleanup">Delete what this run created on the target</option>
              </select></label>
          </div>
        </div>)}

        {step === 5 && (<div className="stack">
          <h2>Review</h2>
          <label className="field" style={{ maxWidth: 420 }}><span>Migration name{sel.length > 1 ? " prefix" : ""}</span>
            <input type="text" value={prefix} onChange={(e) => setPrefix(slug(e.target.value))} /></label>
          <dl className="kv">
            <dt>From</dt><dd>{src?.name} <span className="tag">{src?.type}</span></dd>
            <dt>To</dt><dd>{tgt?.name} <span className="tag">{tgt?.type}</span></dd>
            <dt>VMs</dt><dd>{sel.map((v, i) => <div key={v}>{v} → <code>{names[i]}</code> · {(insp[v] as Inspect).vm.disks.length} disks</div>)}</dd>
            <dt>Data</dt><dd>{totalDisks} disks, {fmt.bytes(totalBytes)}, {parallel} in parallel</dd>
            <dt>Networks</dt><dd>{networks.map((n) => `${n} → ${netMap[n] || "default"}`).join(", ") || "—"}</dd>
            <dt>Snapshot</dt><dd>{appConsistent ? "application-consistent" : "crash-consistent"}</dd>
            {replica && <><dt>Syncs</dt><dd>{cron.trim() ? <><code>{cron}</code> (UTC)</> : "manual"} · keep {retention} restore points</dd></>}
          </dl>
          <div className="alert info">Creating runs a <b>dry-run</b> first: validate endpoints, inspect, re-check disk counts and build the plan.
            Nothing is changed on either platform. You start the real {replica ? "first sync" : "migration"} from the {replica ? "replica" : "migration"} page
            {replica && cron.trim() ? "; the schedule starts firing right away, and its first sync copies every disk" : ""}.</div>
          <ErrorBox error={err} title="Could not create" />
        </div>)}

        <div className="wiz-foot">
          <button className="btn" disabled={step === 0 || busy} onClick={() => setStep(step - 1)}>Back</button>
          {step < STEPS.length - 1
            ? <button className="btn primary" disabled={!canNext} onClick={() => setStep(step + 1)}>Next</button>
            : <button className="btn primary" disabled={busy || !canNext} onClick={create}>
                {busy ? "Creating…" : `Create ${sel.length > 1 ? `${sel.length} ${replica ? "replicas" : "migrations"}` : replica ? "replica" : "migration"} + dry-run`}</button>}
        </div>
      </div>
    </>
  );
}
