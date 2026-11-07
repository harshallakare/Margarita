"""Purpose: Microsoft Hyper-V SourceProvider (standalone host or a cluster node owning the VM).

Usage (endpoint config; password goes in the encrypted credentials):
    type: hyperv
    host: hv01.example.com
    username: EXAMPLE\\migrate               # local Administrators / Hyper-V Administrators on the host
    password: ...
    winrm_transport: ntlm                    # ntlm | kerberos | credssp
    port: 5986                               # WinRM over HTTPS (5985 with use_ssl: false)
    use_ssl: true
    verify_tls: true
    share_map:                               # where THIS worker mounts the VM storage (read-only is enough)
      "C:\\ClusterStorage\\Volume1": /mnt/hv/csv1
      "\\\\fs01\\vmstore": /mnt/hv/vmstore
    rct: false                               # experimental: RCT-based incremental replica reads

How it works:
  inspect  : Get-VM / Get-VMHardDiskDrive / Get-VHD / Get-VMNetworkAdapter / KVP (guest OS) -> IR
  gate     : Get-VMHardDiskDrive count vs WMI Msvm_StorageAllocationSettingData (logical disks)
             + the snapshot's disk count
  refuse   : pass-through disks, shared VHDX / VHD Sets, unknown controllers
  snapshot : WMI recovery snapshot (type 32768); consistency 1 (VSS) if app_consistent else 2.
             The VM's own checkpoint settings are not touched.
  read     : the disk file that was active AT THE SNAPSHOT is frozen while the snapshot exists; it
             is read through share_map with the pure VHDX/VHD readers (unallocated blocks skipped).
             If that file is itself differencing (the VM already had checkpoints) -> refused:
             delete/merge the existing checkpoints first.
  cleanup  : DestroySnapshot of recovery snapshots this run created (merges the .avhdx back)
  RCT      : opt-in (rct: true). End of sync: ConvertToReferencePoint (keeps RCT ids, merges the
             snapshot); next sync: GetVirtualDiskChanges(path, limit_id) per disk. Any RCT error
             makes that disk fall back to compare mode (correct, slower). Default off: unverified.
Never modifies the VM's configuration; never deletes anything it did not create.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from ...core.context import RunContext
from ...core.errors import ChangeTrackingUnavailable, ConfigError, LiveDiskReadRefused, ProviderError, SafetyGateError
from ...core.formats.vhd import open_virtual_disk
from ...core.interfaces import CheckpointRef, DiskReader, SnapshotRef, SourceProvider, VmRef
from ...core.ranges import ByteRange
from ...ir import DiskSpec, VmSpec
from . import mapping


class HypervSource(SourceProvider):
    type_name = "hyperv"

    def __init__(self, name: str, config: Mapping[str, Any], ctx: RunContext, client: Any = None):
        super().__init__(name, config, ctx)
        for k in ("host", "username"):
            if not config.get(k):
                raise ConfigError(f"hyperv source {name}: missing {k!r}")
        self.share_map: dict[str, str] = dict(config.get("share_map") or {})
        self.rct = bool(config.get("rct", False))
        self._client = client
        self._info: dict[str, dict[str, Any]] = {}

    @property
    def client(self) -> Any:
        if self._client is None:
            from .client import HypervClient
            c = self.config
            self._client = HypervClient(c["host"], c["username"], c.get("password", ""),
                                        transport=c.get("winrm_transport", "ntlm"), port=int(c.get("port", 5986)),
                                        use_ssl=bool(c.get("use_ssl", True)), verify_tls=bool(c.get("verify_tls", True)))
        return self._client

    def close(self) -> None:
        if self._client is not None and hasattr(self._client, "close"):
            self._client.close()

    def info(self, vm: VmRef, refresh: bool = False) -> dict[str, Any]:
        if refresh or vm.id not in self._info:
            self._info[vm.id] = self.client.vm_info(vm.id)
        return self._info[vm.id]

    # ------------------------------------------------------------------ contract
    def preflight(self, spec: VmSpec | None = None) -> list[str]:
        try:
            ping = self.client.ping()
        except Exception as e:  # noqa: BLE001
            return [f"cannot reach Hyper-V host {self.config['host']} over WinRM: {e}"]
        p: list[str] = []
        if not ping.get("hyperv"):
            p.append(f"{ping.get('host')}: the Hyper-V PowerShell module is not available")
        if not self.share_map:
            p.append("share_map is empty: map the host's VM storage paths to mounts on this worker")
        if spec is not None:
            for d in spec.disks:
                try:
                    local = mapping.local_path(d.source_handle["path"], self.share_map)
                except mapping.HypervMappingError as e:
                    p.append(str(e))
                    continue
                if not os.path.isdir(os.path.dirname(local)):
                    p.append(f"{d.source_handle['path']} -> {local}: directory not visible on this worker (mount it)")
        return p

    def list_vms(self) -> list[VmRef]:
        return [VmRef(v["id"], v["name"], self.name) for v in self.client.list_vms()]

    def find_vm(self, name_or_id: str) -> VmRef:
        vms = self.client.list_vms()
        hit = [v for v in vms if v["id"].lower() == name_or_id.lower()] or [v for v in vms if v["name"] == name_or_id]
        if not hit:
            raise ProviderError(f"hyperv: no VM {name_or_id!r} on {self.config['host']}")
        if len(hit) > 1:
            raise SafetyGateError(f"hyperv: {len(hit)} VMs are named {name_or_id!r}; use the VM id "
                                  f"({', '.join(v['id'] for v in hit)})")
        return VmRef(hit[0]["id"], hit[0]["name"], self.name)

    def inspect_vm(self, vm: VmRef) -> VmSpec:
        try:
            return mapping.to_vmspec(self.info(vm, refresh=True))
        except mapping.HypervMappingError as e:
            raise SafetyGateError(str(e)) from e

    def disk_count_crosscheck(self, vm: VmRef) -> dict[str, int]:
        return mapping.crosscheck(self.info(vm))

    def create_snapshot(self, vm: VmRef, app_consistent: bool) -> SnapshotRef:
        self.ctx.require_execute("hyper-v recovery snapshot", vm=vm.name, app_consistent=app_consistent)
        spec = mapping.to_vmspec(self.info(vm))
        r = self.client.create_snapshot(vm.id, 1 if app_consistent else 2)
        snap_disks = mapping.as_list(r.get("disks"))
        by_disk: dict[str, dict[str, Any]] = {}
        for d in spec.disks:
            hit = [s for s in snap_disks if mapping.same_path(s["path"], d.source_handle["path"])]
            if len(hit) != 1:
                ref = SnapshotRef(id=r["id"], vm=vm, app_consistent=app_consistent, data={"vm_id": vm.id})
                self.cleanup(ref)   # we created it; don't leave it behind
                raise SafetyGateError(f"snapshot {r['id']}: disk {d.label} ({d.source_handle['path']}) not found "
                                      "among the snapshot's disks — disk layout changed during the snapshot?")
            by_disk[d.id] = hit[0]
        return SnapshotRef(id=r["id"], vm=vm, app_consistent=app_consistent,
                           data={"vm_id": vm.id, "disks": by_disk, "recovery": True})

    def snapshot_disk_count(self, snapshot: SnapshotRef) -> int | None:
        return len(snapshot.data.get("disks") or {})

    def open_disk(self, snapshot: SnapshotRef, disk: DiskSpec) -> DiskReader:
        if not snapshot.id or not snapshot.data.get("recovery"):
            raise LiveDiskReadRefused("hyperv: reads require a vmigrate recovery snapshot")
        sd = (snapshot.data.get("disks") or {}).get(disk.id)
        if sd is None:
            raise SafetyGateError(f"disk {disk.id} missing from snapshot {snapshot.id}")
        if (sd.get("vhd_type") or "").lower() == "differencing" or sd.get("parent"):
            raise SafetyGateError(f"{sd['path']} is a differencing disk (the VM already had checkpoints before ours); "
                                  "delete/merge the existing checkpoints, then retry")
        r = open_virtual_disk(mapping.local_path(sd["path"], self.share_map))
        if r.size < disk.size_bytes:
            r.close()
            raise SafetyGateError(f"{sd['path']}: image size {r.size} < inspected {disk.size_bytes}")
        return r

    def cleanup(self, snapshot: SnapshotRef) -> None:
        self.ctx.require_execute("hyper-v destroy recovery snapshot", snapshot=snapshot.id)
        self.client.destroy_snapshot(snapshot.data.get("vm_id"), snapshot.id)

    # ------------------------------------------------------------------ RCT (experimental)
    def supports_cbt(self) -> bool:
        return self.rct and any(mapping.rct_capable(i) for i in self._info.values())

    def keep_for_cbt(self) -> bool:
        return False   # RCT ids live in a reference point, not in a kept snapshot

    def checkpoint(self, snapshot: SnapshotRef) -> CheckpointRef:
        paths = {k: v["path"] for k, v in (snapshot.data.get("disks") or {}).items()}
        return CheckpointRef(id=snapshot.id, data={"paths": paths, "rct": {}})

    def retire_snapshot(self, snapshot: SnapshotRef, previous: CheckpointRef | None) -> CheckpointRef:
        cp = self.checkpoint(snapshot)
        if not self.supports_cbt():
            self.cleanup(snapshot)
            return cp
        self.ctx.require_execute("hyper-v convert snapshot to RCT reference point", snapshot=snapshot.id)
        try:
            rp = self.client.convert_to_reference_point(snapshot.id)
        except ProviderError as e:      # never leave the snapshot behind; next sync falls back to compare
            self.ctx.logger.warning("RCT reference point failed (%s); removing the snapshot instead", e)
            self.cleanup(snapshot)
            return cp
        ids = mapping.as_list(rp.get("rct_ids"))
        paths = mapping.as_list(rp.get("paths"))
        rct = {}
        if len(ids) == len(paths):
            for did, p in cp.data["paths"].items():
                for path, rid in zip(paths, ids):
                    if mapping.same_path(path, p):
                        rct[did] = rid
        if len(rct) != len(cp.data["paths"]):
            self.ctx.logger.warning("RCT ids could not be matched to every disk; those disks will use compare mode")
        if previous and previous.data.get("reference_point"):
            self.release_checkpoint(previous)
        return CheckpointRef(id=rp["id"], data={**cp.data, "rct": rct, "reference_point": rp["id"]})

    def release_checkpoint(self, checkpoint: CheckpointRef) -> None:
        rp = checkpoint.data.get("reference_point")
        if rp:
            self.ctx.require_execute("hyper-v destroy reference point", reference_point=rp)
            self.client.destroy_reference_point(rp)

    def changed_regions(self, disk: DiskSpec, base: CheckpointRef, cur: CheckpointRef) -> list[ByteRange]:
        limit = (base.data.get("rct") or {}).get(disk.id)
        path = (cur.data.get("paths") or {}).get(disk.id)
        if not limit or not path:
            raise ChangeTrackingUnavailable(f"no RCT id recorded for {disk.label or disk.id}")
        return mapping.changed_ranges(lambda off, n: self.client.changed(path, limit, off, n), disk.size_bytes)
