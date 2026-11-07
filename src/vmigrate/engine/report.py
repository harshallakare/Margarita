"""Purpose: per-VM JSON + Markdown migration reports (and the dry-run plan rendering).

Usage: paths = write_report(state, out_dir, planned=ctx.planned)  -> (json_path, md_path)
"""

from __future__ import annotations

import json
from pathlib import Path

from ..core.context import PlannedAction
from .state import MigrationState


def _verdict(v: dict) -> str:
    if not v:
        return "-"
    out = "PASS" if v.get("ok") else "FAIL"
    full = v.get("full") or {}
    if full.get("skipped"):
        return out + " (full: skipped)"
    if "source_sha256" in full:
        return out + (f" (sha256 {full['source_sha256'][:12]})" if full.get("ok") else " (sha256 MISMATCH)")
    return out


def _gib(n: int | None) -> str:
    return f"{(n or 0) / (1 << 30):,.2f} GiB"


def render_markdown(st: MigrationState, planned: list[PlannedAction] | None = None) -> str:
    spec = st.spec or {}
    lines = [
        f"# Migration report: {st.name}",
        "",
        f"- **Source → target:** `{st.source}` → `{st.target}`",
        f"- **Phase:** `{st.phase.value}` (last good: `{st.last_good_phase.value}`)",
        f"- **VM:** `{spec.get('name', '?')}` — {len(spec.get('disks', []))} disks, "
        f"{_gib(sum(d['size_bytes'] for d in spec.get('disks', [])))} total",
    ]
    if st.crosscheck:
        agree = len(set(st.crosscheck.values())) == 1
        lines.append(f"- **Disk-count cross-check:** {'PASS' if agree else 'FAIL'} `{st.crosscheck}`")
    if st.snapshot:
        lines.append(f"- **Snapshot:** `{st.snapshot.get('id')}` (app-consistent: {st.snapshot.get('app_consistent')})")
    if st.target_vm:
        lines.append(f"- **Target VM:** `{st.target_vm.get('name')}` (`{st.target_vm.get('id')}`)")
    lines += ["", "## Disks", "", "| # | disk | bus | size | copied | MB/s | zero-skipped | committed | attached | verify |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    by_id = {d["id"]: d for d in spec.get("disks", [])}
    for ds in sorted(st.disks.values(), key=lambda d: d.order):
        d = by_id.get(ds.disk_id, {})
        cs = ds.copy_stats or {}
        mbps = f"{cs.get('mb_per_s', 0):.0f}" if cs else ""
        v = ds.verify or {}
        lines.append(
            f"| {ds.order} | `{ds.disk_id}`{' (boot)' if d.get('is_boot') else ''} | {d.get('bus', '')}.{d.get('bus_index', '')} "
            f"| {_gib(ds.size_bytes)} | {'yes' if ds.copied else f'{ds.bytes_copied * 100 // max(ds.size_bytes, 1)}%'} "
            f"| {mbps} | {_gib(cs.get('bytes_zero_skipped'))} | {'yes' if ds.committed else 'no'} "
            f"| {'yes' if ds.attached else 'no'} | {_verdict(v)} |"
        )
    if st.morph:
        lines += ["", "## OSMorph", "", f"- status: `{st.morph.get('status')}`", f"- strategy: `{st.morph.get('strategy')}`"]
        for s in st.morph.get("steps", []):
            lines.append(f"  - {s}")
        for n in st.morph.get("notes", []):
            lines.append(f"  - NOTE: {n}")
    if st.verification:
        lines += ["", "## Verification", "", "```json", json.dumps(st.verification, indent=2), "```"]
    if planned:
        lines += ["", "## Dry-run plan (nothing was changed)", ""]
        for i, a in enumerate(planned, 1):
            det = ", ".join(f"{k}={v}" for k, v in a.details.items())
            lines.append(f"{i}. **{a.action}** {det}")
    if st.errors:
        lines += ["", "## Errors", ""]
        for e in st.errors:
            lines.append(f"- `{e.get('phase')}`: {e.get('error')}")
    return "\n".join(lines) + "\n"


def write_report(st: MigrationState, out_dir: str | Path, planned: list[PlannedAction] | None = None) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    jp = out / f"{st.name}.report.json"
    mp = out / f"{st.name}.report.md"
    data = st.to_dict()
    if planned is not None:
        data["dry_run_plan"] = [{"action": a.action, **a.details} for a in planned]
    jp.write_text(json.dumps(data, indent=2, default=str))
    mp.write_text(render_markdown(st, planned))
    return jp, mp
