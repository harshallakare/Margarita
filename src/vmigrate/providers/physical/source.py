"""Purpose: P2V source — migrate a physical (or any non-hypervisor-managed) Linux server over SSH.

Usage (endpoint config):
    type: physical
    host: 10.0.0.50            ssh_user: root | <user with passwordless sudo>
    ssh_private_key / ssh_known_hosts (credentials; host key pinned)     ssh_port: 22
    mode: cold | lvm-snapshot   (default cold)
    cow_percent: 10             (lvm-snapshot: COW space per snapshot, % of the LV, min 1 GiB)
    remount_ro: true            (lvm-snapshot: plain partitions such as /boot are remounted ro while copying)

Modes:
  cold          the server runs a rescue/live system (or the disks are otherwise unused): raw block
                devices are read directly. Refused while anything on them is mounted read-write.
  lvm-snapshot  the server keeps running: every mounted LVM volume gets an LVM snapshot (device-
                mapper suspends + flushes the filesystem while it is taken), plain partitions like
                /boot are remounted read-only for the copy, and each disk image is composed on the
                fly: LV regions come from the snapshots, the PV metadata area from a copy taken BEFORE
                the snapshots existed (so the target's VG has no trace of them), everything else from
                the raw device. Root on a plain partition, thin/raid/cache LVs, md, LUKS and multiple
                metadata copies are refused (use cold mode). Snapshots of different LVs are taken one
                after the other, so the set is crash-consistent per LV, not across LVs.
Reads go over one SSH pipe per disk (reader.py: zero-skip + zlib). Nothing on the server is
written except the vmigrate-* snapshots, /run/vmigrate/* header copies and the ro remounts, all
undone by cleanup(). UNVERIFIED on real hardware: validate on a test server first.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Mapping
from typing import Any

from ...core.context import RunContext
from ...core.errors import ConfigError, ProviderError, SafetyGateError
from ...core.interfaces import DiskReader, SnapshotRef, SourceProvider, VmRef
from ...ir import DiskSpec, VmSpec
from . import inventory as invmod
from .reader import PipeReader

MODES = ("cold", "lvm-snapshot")
PVSEG_FIELDS = "pv_name,pe_start,vg_extent_size,pv_mda_count,pvseg_start,pvseg_size,lv_name,vg_name,seg_start,vg_free"
LV_FIELDS = "lv_name,vg_name,lv_size,segtype"


def _report(text: str) -> list[dict[str, Any]]:
    rep = (json.loads(text or "{}").get("report") or [{}])[0]
    for v in rep.values():
        if isinstance(v, list):
            return v
    return []


class PhysicalShell:
    """SSH to the server: run commands (argv) and spawn long-lived pipes, sudo when not root."""

    def __init__(self, cfg: Mapping[str, Any], node: Any = None):
        if node is None:
            from ..common.remote import SshNode
            node = SshNode(str(cfg.get("host") or ""), str(cfg.get("ssh_user") or "root"),
                           str(cfg.get("ssh_private_key") or ""), str(cfg.get("ssh_known_hosts") or ""),
                           port=int(cfg.get("ssh_port") or 22), who="physical")
        self.node = node

    def run(self, argv: list[str], *, check: bool = True, timeout: float | None = None) -> str:
        return self.node.run(argv, check=check, timeout=timeout)

    def spawn(self, argv: list[str]) -> subprocess.Popen:
        return self.node.spawn(argv)

    def close(self) -> None:
        self.node.close()


class PhysicalSource(SourceProvider):
    type_name = "physical"

    def __init__(self, name: str, config: Mapping[str, Any], ctx: RunContext, *, shell: Any = None):
        super().__init__(name, config, ctx)
        self.mode = str(config.get("mode") or "cold")
        if self.mode not in MODES:
            raise ConfigError(f"physical: mode must be one of {MODES}")
        self._shell = shell
        self._inv: dict[str, Any] | None = None

    @property
    def shell(self) -> Any:
        if self._shell is None:
            if not self.config.get("host"):
                raise ConfigError("physical: host is required")
            self._shell = PhysicalShell(self.config)
        return self._shell

    def inventory(self, refresh: bool = False) -> dict[str, Any]:
        if self._inv is None or refresh:
            self._inv = json.loads(self.shell.run(["python3", "-c", invmod.INVENTORY]))
        return self._inv

    # ------------------------------------------------------------------ checks
    def check_connection(self) -> list[str]:
        try:
            out = self.shell.run(["sh", "-c", "uname -s; command -v python3 >/dev/null && echo py3 || echo nopy"],
                                 timeout=60)
        except Exception as e:  # noqa: BLE001
            return [f"physical: cannot run commands over SSH on {self.config.get('host')}: {e}"]
        probs = []
        if "Linux" not in out:
            probs.append("physical: only Linux servers are supported")
        if "nopy" in out:
            probs.append("physical: python3 is required on the server (used to inventory and read the disks)")
        return probs

    def preflight(self, spec: VmSpec | None = None) -> list[str]:
        probs = self.check_connection()
        if probs or spec is None:
            return probs
        if self.mode == "lvm-snapshot":
            try:
                self.shell.run(["sh", "-c", "command -v lvcreate"], timeout=60)
            except Exception:  # noqa: BLE001
                probs.append("physical: lvm-snapshot mode needs the LVM tools (lvcreate) on the server")
        return probs

    # ------------------------------------------------------------------ inventory
    def list_vms(self) -> list[VmRef]:
        h = self.inventory()["hostname"]
        return [VmRef(id=h, name=h, provider=self.type_name)]

    def find_vm(self, name_or_id: str) -> VmRef:
        h = self.inventory()["hostname"]
        if name_or_id not in (h, h.split(".")[0], "self", self.config.get("host")):
            raise ProviderError(f"physical: this endpoint is the server {h!r}, not {name_or_id!r}")
        return VmRef(id=h, name=h, provider=self.type_name)

    def inspect_vm(self, vm: VmRef) -> VmSpec:
        spec = invmod.to_vmspec(self.inventory(refresh=True))
        spec.extra["physical"]["mode"] = self.mode
        return spec

    def disk_count_crosscheck(self, vm: VmRef) -> dict[str, int]:
        return invmod.crosscheck(self.inventory())

    # ------------------------------------------------------------------ snapshot
    def create_snapshot(self, vm: VmRef, app_consistent: bool) -> SnapshotRef:
        inv = self.inventory(refresh=True)
        spec = invmod.to_vmspec(inv)
        names = [d.source_handle["kname"] for d in spec.ordered_disks()]
        tag = f"vmigrate-{self.ctx.run_id[:12]}"
        if self.mode == "cold":
            probs = invmod.cold_problems(inv, names)
            if probs:
                raise SafetyGateError("; ".join(probs))
            disks = {d.id: {"base": f"/dev/{d.source_handle['kname']}", "size": d.size_bytes, "overlays": []}
                     for d in spec.disks}
            return SnapshotRef(id=tag, vm=vm, app_consistent=False, data={"mode": "cold", "disks": disks})
        pvsegs = _report(self.shell.run(["pvs", "--segments", "--reportformat", "json", "--units", "b", "--nosuffix",
                                         "-o", PVSEG_FIELDS]))
        lvs = _report(self.shell.run(["lvs", "--reportformat", "json", "--units", "b", "--nosuffix", "-o", LV_FIELDS]))
        plan = invmod.lvm_plan(inv, names, pvsegs, lvs, run_tag=tag, cow_percent=float(self.config.get("cow_percent", 10)),
                               remount_ro=bool(self.config.get("remount_ro", True)))
        self.ctx.require_execute("physical lvm snapshots", host=vm.name, snapshots=[s["name"] for s in plan["snapshots"]],
                                 remount_ro=plan["remount"])
        done: dict[str, Any] = {"mode": "lvm-snapshot", "created": [], "remounted": [], "headers": [], "disks": {}}
        ref = SnapshotRef(id=tag, vm=vm, app_consistent=True, data=done)
        try:
            self.shell.run(["mkdir", "-p", "-m", "0700", "/run/vmigrate"])
            for h in plan["headers"]:                      # BEFORE the snapshots change the VG metadata
                bs = 4096 if h["bytes"] % 4096 == 0 else 512
                self.shell.run(["dd", f"if={h['pv']}", f"of={h['file']}", f"bs={bs}", f"count={h['bytes'] // bs}",
                                "iflag=direct", "status=none"])
                done["headers"].append(h["file"])
            self.shell.run(["sync"])
            for mnt in plan["remount"]:
                self.shell.run(["mount", "-o", "remount,ro", mnt])
                done["remounted"].append(mnt)
            for sn in plan["snapshots"]:
                self.shell.run(["lvcreate", "--snapshot", "--name", sn["name"], "--size", f"{sn['cow_bytes']}b",
                                f"{sn['vg']}/{sn['lv']}"])
                done["created"].append(f"{sn['vg']}/{sn['name']}")
        except Exception:
            self.cleanup(ref)
            raise
        by_k = {d.source_handle["kname"]: d.id for d in spec.disks}
        done["disks"] = {by_k[k]: v for k, v in plan["disks"].items()}
        return ref

    def snapshot_disk_count(self, snapshot: SnapshotRef) -> int | None:
        return len(snapshot.data.get("disks") or {})

    def open_disk(self, snapshot: SnapshotRef, disk: DiskSpec) -> DiskReader:
        m = (snapshot.data.get("disks") or {}).get(disk.id)
        if m is None:
            raise SafetyGateError(f"disk {disk.id} is not part of snapshot {snapshot.id}")
        return PipeReader(self.shell.spawn, dict(m))

    def cleanup(self, snapshot: SnapshotRef) -> None:
        d = snapshot.data
        errs = []
        for name in reversed(list(d.get("created") or [])):
            if "/vmigrate-" not in name:
                continue
            try:
                self.shell.run(["lvremove", "-f", name])
            except Exception as e:  # noqa: BLE001
                errs.append(f"lvremove {name}: {e}")
        for mnt in d.get("remounted") or []:
            try:
                self.shell.run(["mount", "-o", "remount,rw", mnt])
            except Exception as e:  # noqa: BLE001
                errs.append(f"remount rw {mnt}: {e}")
        files = [f for f in d.get("headers") or [] if f.startswith("/run/vmigrate/vmigrate-")]
        if files:
            self.shell.run(["rm", "-f", *files], check=False)
        if errs:
            raise ProviderError("physical cleanup incomplete (fix by hand): " + "; ".join(errs))

    def shutdown_vm(self, vm: VmRef, *, force: bool = False, timeout: int = 900) -> str:
        self.ctx.require_execute("physical poweroff", host=vm.name, force=force)
        cmd = "systemctl poweroff --force" if force else "systemctl poweroff"
        self.shell.run(["sh", "-c", f"nohup sh -c {shlex.quote('sleep 2; ' + cmd)} >/dev/null 2>&1 &"], check=False)
        return f"{vm.name}: {cmd} sent"

    def close(self) -> None:
        if self._shell is not None:
            self._shell.close()
