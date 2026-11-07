"""Purpose: Nutanix AHV SourceProvider (the reference vertical's source side).

Usage (config):
    sources:
      ahv1:
        type: nutanix
        prism_element: https://10.0.0.10:9440      # v1/v2 (PD snapshots, vm_disk_info)
        prism_central: https://10.0.0.20:9440      # v3 (optional; defaults to prism_element)
        username: admin
        password: ${NUTANIX_PASSWORD}
        verify_tls: false
        protection_domain: vmigrate-pd             # PD used for app-consistent OOB snapshots
        protect_vm_if_missing: false               # add VM to the PD automatically (PD config change)
        snapshot_retention_secs: 172800
        nfs_mounts: {ctr-prod: /mnt/ntnx/ctr-prod} # container name -> local NFS mount (whitelisted)
        # or nfs_root: /mnt/ntnx                   # local path = nfs_root + ndfs_filepath

How it works:
  inspect  : Prism v3 VM (paged list, full disk_list) joined with v2 vm_disk_info; both must agree
  gate     : disk_count_crosscheck = {prism_v3.disk_list, prism_v2.vm_disk_info}
  snapshot : v1 PD oob_schedules (appConsistent) -> v2 dr_snapshots poll -> v2 restore_entities as a
             powered-off clone VM "vmigrate-<run>-<name>" whose vmdisks are the frozen copy
  read     : clone's vmdisk files over NFS (<mount>/.acropolis/vmdisk/<uuid>), plain sequential
             pread in large blocks — NEVER qemu-img / hole probing on NFS (it hangs, pitfall 3)
  cleanup  : delete ONLY the clone VM + DR snapshot recorded in the SnapshotRef (guarded)
The source VM itself is never modified, powered off, or deleted.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...core.context import RunContext
from ...core.errors import ConfigError, LiveDiskReadRefused, ProviderError, SafetyGateError
from ...core.fileio import RawFileReader
from ...core.interfaces import DiskReader, SnapshotRef, SourceProvider, VmRef
from ...core.retry import poll_until
from ...ir import DiskSpec, VmSpec
from .client import PrismClient
from .mapping import disks_v2, disks_v3, to_vmspec

PAGE = 250
V2_DISK_PARAMS = {"include_vm_disk_config": "true", "include_vm_nic_config": "true"}


class NutanixSource(SourceProvider):
    type_name = "nutanix"

    def __init__(self, name: str, config: Mapping[str, Any], ctx: RunContext, client: PrismClient | None = None):
        super().__init__(name, config, ctx)
        for k in ("prism_element", "username"):
            if k not in config and client is None:
                raise ConfigError(f"sources.{name}: missing {k!r}")
        self.c = client or PrismClient(
            pe_url=config["prism_element"], pc_url=config.get("prism_central"),
            username=config["username"],
            password=config.get("password") or os.environ.get(config.get("password_env", ""), ""),
            verify_tls=config.get("verify_tls", True),
        )
        self.pd = config.get("protection_domain")
        self.poll_interval = float(config.get("poll_interval", 10))
        self.snapshot_timeout = float(config.get("snapshot_timeout", 3600))

    # ------------------------------------------------------------------ inventory
    def _v3_all_vms(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            r = self.c.v3_post("/vms/list", {"kind": "vm", "length": PAGE, "offset": offset})
            ents = r.get("entities") or []
            out += ents
            total = (r.get("metadata") or {}).get("total_matches", len(out))
            offset += len(ents)
            if not ents or offset >= total:
                return out

    def list_vms(self) -> list[VmRef]:
        return [VmRef(e["metadata"]["uuid"], (e.get("status") or e.get("spec"))["name"], self.name)
                for e in self._v3_all_vms()]

    def find_vm(self, name_or_id: str) -> VmRef:
        hits = [v for v in self.list_vms() if name_or_id in (v.id, v.name)]
        if len(hits) != 1:
            raise ProviderError(f"nutanix: expected exactly 1 VM matching {name_or_id!r}, found {len(hits)}: {hits}")
        return hits[0]

    def _v3(self, vm: VmRef) -> dict[str, Any]:
        return self.c.v3_get(f"/vms/{vm.id}")

    def _v2(self, uuid: str) -> dict[str, Any]:
        return self.c.v2_get(f"/vms/{uuid}", params=V2_DISK_PARAMS)

    def inspect_vm(self, vm: VmRef) -> VmSpec:
        spec = to_vmspec(self._v3(vm), self._v2(vm.id))
        spec.extra["nutanix"]["containers"] = sorted({self._container_of(d) for d in spec.disks})
        return spec

    def disk_count_crosscheck(self, vm: VmRef) -> dict[str, int]:
        # Two independent API generations, fetched fresh (not the objects inspect_vm used).
        return {"prism_v3.disk_list": len(disks_v3(self._v3(vm))),
                "prism_v2.vm_disk_info": len(disks_v2(self._v2(vm.id)))}

    # ------------------------------------------------------------------ preflight
    @staticmethod
    def _container_of(d: DiskSpec) -> str:
        p = d.source_handle.get("ndfs_filepath") or ""
        parts = p.strip("/").split("/")
        return parts[0] if parts and parts[0] else ""

    def _local_path(self, ndfs_filepath: str) -> Path:
        parts = ndfs_filepath.strip("/").split("/", 1)
        if len(parts) != 2:
            raise ProviderError(f"unexpected ndfs_filepath {ndfs_filepath!r}")
        mounts = self.config.get("nfs_mounts") or {}
        if parts[0] in mounts:
            return Path(mounts[parts[0]]) / parts[1]
        if self.config.get("nfs_root"):
            return Path(self.config["nfs_root"]) / parts[0] / parts[1]
        raise ConfigError(f"sources.{self.name}: no nfs_mounts entry for container {parts[0]!r} (and no nfs_root)")

    def preflight(self, spec: VmSpec | None = None) -> list[str]:
        problems: list[str] = []
        if not self.pd:
            problems.append(f"sources.{self.name}.protection_domain is not set (needed for app-consistent snapshots)")
        if spec is not None:
            for ctr in spec.extra.get("nutanix", {}).get("containers", []):
                try:
                    root = self._local_path(f"/{ctr}/.acropolis")
                except (ConfigError, ProviderError) as e:
                    problems.append(str(e))
                    continue
                if not root.parent.is_dir():
                    problems.append(f"NFS mount for container {ctr!r} not present at {root.parent} "
                                    "(mount it and whitelist this host on the Nutanix cluster)")
        return problems

    # ------------------------------------------------------------------ snapshot
    def _ensure_protected(self, vm: VmRef, app: bool) -> None:
        pd = self.c.v2_get(f"/protection_domains/{self.pd}")
        if any(v.get("vm_id") == vm.id for v in pd.get("vms") or []):
            return
        if not self.config.get("protect_vm_if_missing", False):
            raise ProviderError(f"VM {vm.name} is not in protection domain {self.pd!r}; add it or set "
                                "protect_vm_if_missing: true")
        self.ctx.require_execute("nutanix protect vm in PD", pd=self.pd, vm=vm.name)
        self.c.v2_post(f"/protection_domains/{self.pd}/protect_vms",
                       {"app_consistent_snapshots": app, "uuids": [vm.id]})

    def create_snapshot(self, vm: VmRef, app_consistent: bool) -> SnapshotRef:
        self.ctx.require_execute("nutanix PD oob snapshot", pd=self.pd, vm=vm.name, app_consistent=app_consistent)
        if not self.pd:
            raise ConfigError(f"sources.{self.name}.protection_domain is required")
        self._ensure_protected(vm, app_consistent)
        t0 = int(time.time() * 1e6) - 60_000_000  # 60s clock-skew allowance
        self.c.v1_post(f"/protection_domains/{self.pd}/oob_schedules", {
            "appConsistent": app_consistent,
            "snapshotRetentionTimeSecs": int(self.config.get("snapshot_retention_secs", 172800)),
        })

        def newest() -> dict[str, Any] | None:
            ents = self.c.v2_get(f"/protection_domains/{self.pd}/dr_snapshots").get("entities") or []
            mine = [e for e in ents if int(e.get("snapshot_create_time_usecs", 0)) >= t0
                    and any(v.get("vm_id") == vm.id for v in e.get("vms") or [])]
            return max(mine, key=lambda e: int(e["snapshot_create_time_usecs"])) if mine else None

        snap = poll_until(newest, lambda e: e is not None and e.get("state", "AVAILABLE") == "AVAILABLE",
                          timeout=self.snapshot_timeout, interval=self.poll_interval, what="PD snapshot")
        sid = str(snap["snapshot_id"])
        prefix = f"vmigrate-{self.ctx.run_id}-"
        clone_name = prefix + vm.name
        ref_data: dict[str, Any] = {"protection_domain": self.pd, "snapshot_id": sid, "clone_name": clone_name,
                                    "app_consistent_reported": snap.get("app_consistent_snapshots")}
        # Materialize the frozen copy as a powered-off clone VM; its vmdisks are what we read.
        self.ctx.require_execute("nutanix restore snapshot as clone", snapshot=sid, clone=clone_name)
        self.c.v2_post(f"/protection_domains/{self.pd}/restore_entities", {
            "snapshot_id": sid, "vm_name_prefix": prefix, "vm_uuids": [vm.id],
            "replace": False, "restore_network_configuration": False,
        })

        def find_clone() -> dict[str, Any] | None:
            ents = self.c.v2_get("/vms/", params={"filter": f"vm_name=={clone_name}", **V2_DISK_PARAMS}).get("entities") or []
            return ents[0] if len(ents) == 1 else None

        clone = poll_until(find_clone, lambda e: e is not None, timeout=self.snapshot_timeout,
                           interval=self.poll_interval, what=f"clone VM {clone_name}")
        clone_uuid = clone["uuid"]
        if clone_uuid == vm.id:
            raise SafetyGateError("clone VM uuid equals source VM uuid — refusing")
        clone = self._v2(clone_uuid)
        if (clone.get("power_state") or "off").lower() != "off":
            raise SafetyGateError(f"clone {clone_name} is powered on; its disks are not frozen")
        live = {d.vmdisk_uuid for d in disks_v2(self._v2(vm.id))}
        cdisks = disks_v2(clone)
        src_by_addr = {(d.bus, d.index): d for d in disks_v2(self._v2(vm.id))}
        paths: dict[str, str] = {}
        for cd in cdisks:
            sd = src_by_addr.get((cd.bus, cd.index))
            if sd is None:
                raise SafetyGateError(f"clone disk {cd.bus.value}.{cd.index} has no source counterpart")
            if cd.vmdisk_uuid in live:
                raise LiveDiskReadRefused(f"clone disk {cd.vmdisk_uuid} is a live source vmdisk")
            if cd.size_bytes != sd.size_bytes:
                raise SafetyGateError(f"clone disk {cd.bus.value}.{cd.index} size {cd.size_bytes} != source {sd.size_bytes}")
            paths[sd.vmdisk_uuid] = cd.ndfs_filepath
        ref_data.update({"clone_uuid": clone_uuid, "disk_paths": paths, "live_vmdisks": sorted(live)})
        return SnapshotRef(id=sid, vm=vm, app_consistent=app_consistent, data=ref_data)

    def snapshot_disk_count(self, snapshot: SnapshotRef) -> int | None:
        return len(snapshot.data.get("disk_paths", {}))

    def open_disk(self, snapshot: SnapshotRef, disk: DiskSpec) -> DiskReader:
        paths = snapshot.data.get("disk_paths") or {}
        ndfs = paths.get(disk.id)
        if not ndfs:
            raise LiveDiskReadRefused(f"disk {disk.id} ({disk.address}) has no snapshot copy in {snapshot.id}")
        if Path(ndfs).name in set(snapshot.data.get("live_vmdisks", [])):
            raise LiveDiskReadRefused(f"{ndfs} is a live source vmdisk")
        local = self._local_path(ndfs)
        if not local.exists():
            raise ProviderError(f"snapshot disk file not visible over NFS: {local}")
        return RawFileReader(local, size=disk.size_bytes)

    # ------------------------------------------------------------------ cleanup
    def cleanup(self, snapshot: SnapshotRef) -> None:
        d = snapshot.data
        self.ctx.require_execute("nutanix cleanup clone + snapshot", clone=d.get("clone_name"), snapshot=snapshot.id)
        cu = d.get("clone_uuid")
        if cu:
            if cu == snapshot.vm.id or not str(d.get("clone_name", "")).startswith("vmigrate-"):
                raise SafetyGateError(f"refusing to delete VM {cu}: not a vmigrate clone")
            cur = self._v2(cu)
            if cur.get("name") != d.get("clone_name"):
                raise SafetyGateError(f"refusing to delete VM {cu}: name {cur.get('name')!r} != recorded clone")
            self.c.v2_delete(f"/vms/{cu}")
        if d.get("snapshot_id") and d.get("protection_domain"):
            self.c.v2_delete(f"/protection_domains/{d['protection_domain']}/dr_snapshots/{d['snapshot_id']}")
