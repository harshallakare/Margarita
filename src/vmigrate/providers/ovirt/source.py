"""Purpose: oVirt / Red Hat Virtualization / Oracle Linux Virtualization Manager SourceProvider,
using the engine's backup API (4.4+) and ImageIO. No SDK, no agent on the hosts.

Usage (endpoint config; the password goes in the encrypted credentials):
    type: ovirt
    url: https://engine.example.com            # /ovirt-engine/api is appended
    username: admin@internal                    # user@profile
    password: ...                               # credential
    ca_pem: "-----BEGIN CERTIFICATE-----..."    # the engine CA (https://engine/ovirt-engine/services/pki-resource?resource=ca-certificate&format=X509-PEM-CA)
    verify_tls: true
    use_proxy: false                            # true: read through the engine proxy (54323) instead of the hosts (54322)
    read_workers: 8

How it works:
  inspect  : /vms/{id} + diskattachments + /disks/{id} + NICs (vNIC profile -> network) -> IR
  gate     : disk attachments vs /disks?search=vm_names=<name>
  refuse   : deactivated attachments, direct LUNs, managed block storage disks
  snapshot : POST /vms/{id}/backups for every disk (running or shut down; cold backup needs 4.4.6+).
             The engine creates a checkpoint (dirty bitmaps, on disks with incremental backup
             enabled). app_consistent sets require_consistency=true, so the backup fails rather
             than continuing when the guest agent cannot freeze the filesystems (4.5+).
  read     : one ImageIO download transfer per open disk; parallel 8 MiB range reads; zero
             extents skipped on a fresh copy; the transfer is finalized when the reader closes
  CBT      : the backup is incremental from the VM's newest checkpoint; changed ranges come from
             ImageIO extents?context=dirty. If that checkpoint is not the replica's base (another
             backup tool made one meanwhile) the sync falls back to compare mode.
  cleanup  : finalize the backup; checkpoints that Margarita created are removed when they are the
             chain's root (oVirt only removes the oldest checkpoint)
The source VM and its disks are never changed except by the opt-in cutover shutdown.

Tested against a fake engine and ImageIO only; NOT run against a real engine yet.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

from ...core.context import RunContext
from ...core.errors import ChangeTrackingUnavailable, ConfigError, LiveDiskReadRefused, ProviderError, SafetyGateError
from ...core.interfaces import CheckpointRef, DiskReader, SnapshotRef, SourceProvider, VmRef
from ...core.ranges import ByteRange
from ...ir import DiskSpec, VmSpec
from . import mapping
from .imageio import ImageioReader, extents

PREFIX = "vmigrate-"


class OvirtSource(SourceProvider):
    type_name = "ovirt"

    def __init__(self, name: str, config: Mapping[str, Any], ctx: RunContext, api: Any = None):
        super().__init__(name, config, ctx)
        for k in ("url", "username"):
            if not config.get(k):
                raise ConfigError(f"ovirt source {name}: missing {k!r}")
        self._api = api
        self._vm: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    @property
    def api(self) -> Any:
        if self._api is None:
            from .api import OvirtApi
            c = self.config
            self._api = OvirtApi(c["url"], c["username"], c.get("password", ""), ca_pem=c.get("ca_pem"),
                                 verify_tls=bool(c.get("verify_tls", True)))
        return self._api

    # ------------------------------------------------------------------ inventory
    def _load(self, vm: VmRef) -> dict[str, Any]:
        v = self.api.vm(vm.id)
        atts = self.api.disk_attachments(vm.id)
        disks = {}
        for a in atts:
            did = (a.get("disk") or {}).get("id") or a.get("id")
            disks[did] = self.api.disk(did)
        nics = self.api.nics(vm.id)
        nets = {}
        for n in nics:
            pid = (n.get("vnic_profile") or {}).get("id")
            if pid and pid not in nets:
                nets[pid] = self.api.network_of_profile(pid)
        d = {"vm": v, "atts": atts, "disks": disks, "nics": nics, "nets": nets}
        self._vm[vm.id] = d
        return d

    def _spec(self, d: dict[str, Any]) -> VmSpec:
        try:
            return mapping.to_vmspec(d["vm"], d["atts"], d["disks"], d["nics"], d["nets"])
        except mapping.OvirtMappingError as e:
            raise SafetyGateError(str(e)) from e

    def check_connection(self) -> list[str]:
        try:
            self.api.version()
            self.api.vms()
        except Exception as e:  # noqa: BLE001
            return [f"cannot use the oVirt engine at {self.config['url']} as {self.config['username']}: {e}"]
        return []

    def preflight(self, spec: VmSpec | None = None) -> list[str]:
        try:
            self.api.version()
        except Exception as e:  # noqa: BLE001
            return [f"cannot reach the oVirt engine {self.config['url']}: {e}"]
        return []

    def list_vms(self) -> list[VmRef]:
        return [VmRef(v["id"], v.get("name") or v["id"], self.name) for v in self.api.vms()]

    def find_vm(self, name_or_id: str) -> VmRef:
        vms = self.api.vms()
        hit = [v for v in vms if v.get("id") == name_or_id] or [v for v in vms if v.get("name") == name_or_id]
        if not hit:
            raise ProviderError(f"ovirt: no VM {name_or_id!r}")
        return VmRef(hit[0]["id"], hit[0].get("name") or hit[0]["id"], self.name)   # names are unique in oVirt

    def inspect_vm(self, vm: VmRef) -> VmSpec:
        return self._spec(self._load(vm))

    def disk_count_crosscheck(self, vm: VmRef) -> dict[str, int]:
        d = self._vm.get(vm.id) or self._load(vm)
        return mapping.crosscheck(d["atts"], self.api.disks_by_vm_name(d["vm"].get("name") or vm.name))

    def shutdown_vm(self, vm: VmRef, *, force: bool = False, timeout: int = 900) -> str:
        if str(self.api.vm(vm.id).get("status")) == "down":
            return f"{vm.name} already down"
        self.ctx.require_execute("ovirt stop" if force else "ovirt shutdown", vm=vm.id)
        self.api.shutdown(vm.id, force=force)
        return f"{vm.name} {'stopped' if force else 'shutdown requested'}"

    # ------------------------------------------------------------------ backups
    def create_snapshot(self, vm: VmRef, app_consistent: bool) -> SnapshotRef:
        d = self._load(vm)
        spec = self._spec(d)
        tag = f"{PREFIX}{self.ctx.run_id}"
        base = mapping.leaf_checkpoint(self.api.checkpoints(vm.id))
        self.ctx.require_execute("ovirt backup", vm=vm.id, disks=len(spec.disks), from_checkpoint=base,
                                 require_consistency=app_consistent)
        ids = [x.id for x in spec.ordered_disks()]
        try:
            b = self.api.start_backup(vm.id, ids, base, tag, app_consistent)
        except ProviderError as e:
            if not base:
                raise
            self.ctx.logger.warning("ovirt: incremental backup from %s refused (%s); taking a full one", base, e)
            base = None
            b = self.api.start_backup(vm.id, ids, None, tag, app_consistent)
        ref = SnapshotRef(id=tag, vm=vm, app_consistent=app_consistent,
                          data={"vm_id": vm.id, "backup_id": b["id"], "from_checkpoint": base, "checkpoint": None})
        try:
            ready = self.api.wait_backup(vm.id, b["id"])
        except Exception:
            self.api.finalize_backup(vm.id, b["id"])
            raise
        ref.data["checkpoint"] = ready.get("to_checkpoint_id")
        return ref

    def _transfer(self, snapshot: SnapshotRef | CheckpointRef, disk: DiskSpec) -> tuple[str, str]:
        t = self.api.start_transfer(disk.id, snapshot.data["backup_id"])
        url = t.get("proxy_url") if self.config.get("use_proxy") else (t.get("transfer_url") or t.get("proxy_url"))
        if not url:
            self.api.finalize_transfer(t["id"])
            raise ProviderError(f"ovirt: image transfer for {disk.label} has no URL")
        return t["id"], url

    def open_disk(self, snapshot: SnapshotRef, disk: DiskSpec) -> DiskReader:
        if not snapshot.id.startswith(PREFIX) or not snapshot.data.get("backup_id"):
            raise LiveDiskReadRefused("ovirt: reads require a vmigrate backup")
        tid, url = self._transfer(snapshot, disk)
        return ImageioReader(url, disk.size_bytes, http=self.api.http, workers=int(self.config.get("read_workers", 8)),
                             on_close=lambda: self.api.finalize_transfer(tid))

    def _drop_checkpoint(self, vm_id: str, cid: str | None) -> None:
        if not cid:
            return
        cps = self.api.checkpoints(vm_id)
        if not any(c.get("id") == cid for c in cps):
            return
        if mapping.root_checkpoint(cps) == cid:
            self.api.delete_checkpoint(vm_id, cid)
        else:
            self.ctx.logger.warning("ovirt: checkpoint %s is not the oldest of the VM's chain; oVirt only removes "
                                    "the oldest, so it stays (harmless; delete it later from the oldest up)", cid)

    def cleanup(self, snapshot: SnapshotRef) -> None:
        self.ctx.require_execute("ovirt finalize backup", vm=snapshot.data.get("vm_id"), backup=snapshot.data.get("backup_id"))
        self.api.finalize_backup(snapshot.data["vm_id"], snapshot.data["backup_id"])
        self._drop_checkpoint(snapshot.data["vm_id"], snapshot.data.get("checkpoint"))

    # ------------------------------------------------------------------ CBT
    def supports_cbt(self) -> bool:
        return True

    def keep_for_cbt(self) -> bool:
        return False          # the backup is finalized after each sync; its checkpoint persists

    def checkpoint(self, snapshot: SnapshotRef) -> CheckpointRef:
        return CheckpointRef(id=snapshot.data.get("checkpoint") or "", data=dict(snapshot.data))

    def retire_snapshot(self, snapshot: SnapshotRef, previous: CheckpointRef | None) -> CheckpointRef:
        cp = self.checkpoint(snapshot)
        self.api.finalize_backup(snapshot.data["vm_id"], snapshot.data["backup_id"])
        if previous is not None and previous.id and previous.id != cp.id:
            self._drop_checkpoint(snapshot.data["vm_id"], previous.id)
        return cp

    def release_checkpoint(self, checkpoint: CheckpointRef) -> None:
        self._drop_checkpoint(checkpoint.data.get("vm_id", ""), checkpoint.id)

    def changed_regions(self, disk: DiskSpec, base: CheckpointRef, cur: CheckpointRef) -> list[ByteRange]:
        if not base.id or cur.data.get("from_checkpoint") != base.id:
            raise ChangeTrackingUnavailable(f"backup is incremental from {cur.data.get('from_checkpoint')}, "
                                            f"not the replica's base {base.id or '(none)'}")
        if str(disk.source_handle.get("backup")) != "incremental":
            raise ChangeTrackingUnavailable(f"{disk.label}: incremental backup is not enabled on this disk")
        tid, url = self._transfer(cur, disk)
        try:
            return extents(self.api.http, url, "dirty", disk.size_bytes)
        except ProviderError as e:
            raise ChangeTrackingUnavailable(f"{disk.label}: dirty extents: {e}") from e
        finally:
            self.api.finalize_transfer(tid)
