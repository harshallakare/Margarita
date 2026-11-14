"""Purpose: Nutanix AHV SourceProvider (the reference vertical's source side).

Usage (config):
    sources:
      ahv1:
        type: nutanix
        prism_element: https://10.0.0.10:9440      # v1/v2 (PD snapshots, vm_disk_info)
        prism_central: https://10.0.0.20:9440      # v3 + v4 (needed for the iSCSI transport)
        transport: auto                            # auto | iscsi | nfs (auto: iSCSI when PC v4 + a cluster
                                                   # Data Services IP are available, else NFS)
        username: admin
        password: ${NUTANIX_PASSWORD}
        verify_tls: false
        protection_domain: vmigrate-pd             # NFS transport only: PD used for OOB snapshots
        protect_vm_if_missing: false               # add VM to the PD automatically (PD config change)
        snapshot_retention_secs: 172800
        crt: true                                  # replicas: read only changed regions (iSCSI transport)
        crt_base_keep_hours: 336                   # how long the kept base recovery point lives (> sync interval)
        crt_api_version: v4.0                      # dataprotection API version for the CRT calls
        # NFS access is AUTOMATIC: the worker mounts each needed container read-only from the
        # cluster (<prism_element host>:/<container>) and unmounts it afterwards. One-time cluster
        # step: add the worker's IP to the NFS whitelist. Optional overrides:
        # nfs_host: 10.0.0.11                      # NFS address if not the Prism Element host
        # nfs_mounts: {ctr-prod: /mnt/ntnx/ctr-prod}   # use these pre-mounted paths instead
        # nfs_root: /mnt/ntnx                      # or: pre-mounted under nfs_root/<container>

How it works (iSCSI transport, PC >= 7.3 / AOS >= 6.8 — the Coriolis method, no NFS, no PD):
  snapshot : v4 recovery point (app/crash consistent) -> temporary volume group vmigrate-<run> with one
             disk per VM disk, data source = the disk recovery point (instant, no copy) -> this
             worker's iSCSI initiator attached through the API (no manual whitelist)
  read     : iscsiadm login to the cluster Data Services IP; each LUN mapped to its disk and size-
             checked; the volume group's disk count and the visible LUN count join the disk gate
  cleanup  : iSCSI logout -> detach initiator -> delete volume group -> delete recovery point
  replicas : CRT (changed region tracking). At the end of a sync the volume group and iSCSI session go,
             but the recovery point is KEPT (expiry extended to crt_base_keep_hours) as the reference;
             the next sync asks Prism for the regions that differ between the new and the kept recovery
             point (discover-cluster on PC -> compute-changed-regions on the cluster with a 15-min JWT)
             and reads only those. The old reference is then deleted; deleting the replica deletes the
             last one. Any CRT error (base expired, unsupported AOS) -> that disk uses compare mode.
             The NFS transport has no CRT: compare mode.
How it works (NFS transport, any cluster; the manually proven path):
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
from urllib.parse import urlparse

from ...core import cancel
from ...core.context import RunContext
from ...core.errors import ChangeTrackingUnavailable, ConfigError, LiveDiskReadRefused, ProviderError, SafetyGateError
from ...core.fileio import RawFileReader
from ...core.interfaces import CheckpointRef, DiskReader, SnapshotRef, SourceProvider, VmRef
from ...core.ranges import ByteRange, normalize
from ...core.retry import poll_until
from ...ir import DiskSpec, VmSpec
from .client import PrismClient
from .iscsi import IscsiSession, block_size_of, map_devices
from .nfs import NfsMounter
from .v4 import PrismV4
from .mapping import disks_v2, disks_v3, to_vmspec

class LunReader(RawFileReader):
    """An iSCSI LUN of the recovery point; knows which regions the guest wrote (via `allocated`)."""

    def __init__(self, path: str, size: int, allocated: Any):
        super().__init__(path, size=size)
        self._allocated = allocated

    def allocated_ranges(self) -> list[ByteRange] | None:
        return self._allocated()


PAGE = 250
V2_DISK_PARAMS = {"include_vm_disk_config": "true", "include_vm_nic_config": "true"}


class NutanixSource(SourceProvider):
    type_name = "nutanix"

    def __init__(self, name: str, config: Mapping[str, Any], ctx: RunContext, client: PrismClient | None = None,
                 mounter: NfsMounter | None = None, v4: PrismV4 | None = None, iscsi_factory: Any = None,
                 size_of: Any = None):
        super().__init__(name, config, ctx)
        self._mounter = mounter
        self.transport = config.get("transport", "auto")
        if self.transport not in ("auto", "iscsi", "nfs"):
            raise ConfigError(f"sources.{name}.transport must be auto, iscsi or nfs")
        self._v4 = v4
        self._iscsi_factory = iscsi_factory or IscsiSession
        self._size_of = size_of or block_size_of
        self._sessions: dict[str, IscsiSession] = {}
        self._decided: dict[str, tuple[str, str]] = {}
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

    # ------------------------------------------------------------------ connection
    def check_connection(self) -> list[str]:
        # interactive save-time check: one attempt, fail fast (migrations keep the client's retries)
        if hasattr(self.c, "attempts"):
            self.c.attempts = 1
        p: list[str] = []
        try:
            cl = self.c.v2_get("/cluster") or {}
        except Exception as e:  # noqa: BLE001 - surface any reachability/auth problem
            return [f"cannot reach Prism Element {self.config.get('prism_element')}: {e}"]
        if self.config.get("prism_central"):
            try:
                self.c.v3_post("/clusters/list", {"kind": "cluster", "length": 1})
            except Exception as e:  # noqa: BLE001
                p.append(f"cannot reach Prism Central {self.config['prism_central']}: {e}")
        v4_ok, why = self._v4_status()
        if self.transport == "iscsi" and not v4_ok:
            p.append(f"transport iscsi: {why}")
        needs_pd = self.transport == "nfs" or (self.transport == "auto" and not v4_ok)
        if not needs_pd:
            return p
        if not self.pd:
            p.append("protection_domain is required for the NFS transport (Prism Central v4 not available: "
                     f"{why})")
        else:
            try:
                self.c.v2_get(f"/protection_domains/{self.pd}")
            except Exception as e:  # noqa: BLE001
                p.append(f"protection domain {self.pd!r} not found on cluster {cl.get('name', '?')}: {e}")
        return p

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
        return self.mounter.path_for(parts[0]) / parts[1]    # default: mount it ourselves

    @property
    def mounter(self) -> NfsMounter:
        if self._mounter is None:
            host = self.config.get("nfs_host") or urlparse(str(self.config.get("prism_element", ""))).hostname
            if not host:
                raise ConfigError(f"sources.{self.name}: cannot derive the NFS host; set nfs_host")
            self._mounter = NfsMounter(host)
        return self._mounter

    def close(self) -> None:
        for s in list(self._sessions.values()):
            s.logout()
        self._sessions.clear()
        if self._mounter is not None:
            self._mounter.unmount_all()

    # ------------------------------------------------------------------ transport choice
    @property
    def v4(self) -> PrismV4:
        if self._v4 is None:
            if not self.config.get("prism_central"):
                raise ConfigError("the iSCSI transport needs prism_central (Prism Central v4 API)")
            self._v4 = PrismV4(self.c.s, self.config["prism_central"],
                               crt_version=str(self.config.get("crt_api_version", "v4.0")))
        return self._v4

    def _v4_status(self) -> tuple[bool, str]:
        if self.transport == "nfs":
            return False, "transport is nfs"
        if not self.config.get("prism_central") and self._v4 is None:
            return False, "prism_central is not set"
        try:
            self.v4.clusters()
            return True, "Prism Central v4 API reachable"
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    def transport_for(self, cluster_ext: str | None) -> tuple[str, str]:
        """('iscsi'|'nfs', reason) for VMs on `cluster_ext`. Explicit config wins; auto prefers iSCSI."""
        key = cluster_ext or ""
        if key in self._decided:
            return self._decided[key]
        if self.transport == "nfs":
            out = ("nfs", "configured")
        else:
            ok, why = self._v4_status()
            dsip = self.v4.data_services_ip(cluster_ext) if ok and cluster_ext else None
            if ok and dsip:
                out = ("iscsi", f"Prism Central v4 + Data Services IP {dsip}")
            elif self.transport == "iscsi":
                raise ProviderError(f"transport iscsi unavailable: {why if not ok else 'the cluster has no Data Services IP'}")
            else:
                out = ("nfs", f"iSCSI unavailable ({why if not ok else 'no Data Services IP on the cluster'})")
        self._decided[key] = out
        return out

    def preflight(self, spec: VmSpec | None = None) -> list[str]:
        problems: list[str] = []
        why_nfs = "transport is nfs"
        if spec is None and self.transport != "nfs":
            # endpoint-level check (no VM yet): with Prism Central v4 reachable, iSCSI is used and no
            # NFS settings are needed. The per-cluster decision (Data Services IP) is made per VM.
            ok, why = self._v4_status()
            if ok:
                return problems
            if self.transport == "iscsi":
                return [f"transport iscsi: {why}"]
            why_nfs = f"auto transport fell back to NFS because iSCSI is unavailable: {why}"
        if spec is not None:
            try:
                mode, why = self.transport_for((spec.extra.get("nutanix") or {}).get("cluster"))
            except ProviderError as e:
                return [str(e)]
            self.ctx.logger.info("nutanix transport for %s: %s (%s)", spec.name, mode, why)
            why_nfs = f"transport for {spec.name}: nfs ({why})"
            if mode == "iscsi":
                try:
                    self._iscsi_factory("127.0.0.1").initiator_name()
                except ProviderError as e:
                    problems.append(str(e))
                return problems
        if not self.pd:
            problems.append(f"sources.{self.name}.protection_domain is not set, needed by the NFS transport "
                            f"[{why_nfs}]. To avoid NFS, set Prism Central on the endpoint (PC 7.3+ with the "
                            "v4 API and a cluster Data Services IP).")
        if spec is not None:
            for ctr in spec.extra.get("nutanix", {}).get("containers", []):
                try:
                    root = self._local_path(f"/{ctr}/.acropolis")
                except (ConfigError, ProviderError) as e:
                    problems.append(str(e))
                    continue
                if not root.is_dir():
                    problems.append(f"container {ctr!r} is not readable at {root.parent} (no .acropolis directory); "
                                    "check the NFS export and this worker's whitelist entry on the Nutanix cluster")
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

    def shutdown_vm(self, vm: VmRef, *, force: bool = False, timeout: int = 900) -> str:
        """Prism v2 set_power_state ACPI_SHUTDOWN (needs NGT or an ACPI-aware guest) / OFF."""
        if str(self.client.v2_get(f"/vms/{vm.id}").get("power_state", "")).lower() == "off":
            return f"{vm.name} already off"
        how = "OFF" if force else "ACPI_SHUTDOWN"
        self.ctx.require_execute("nutanix power off", vm=vm.name, transition=how)
        self.client.v2_post(f"/vms/{vm.id}/set_power_state", {"transition": how})
        deadline = time.monotonic() + timeout
        while str(self.client.v2_get(f"/vms/{vm.id}").get("power_state", "")).lower() != "off":
            if time.monotonic() > deadline:
                raise ProviderError(f"{vm.name}: still on {timeout}s after {how}; retry with force_power_off")
            cancel.sleep(5)
        return f"{vm.name} powered off ({how})"

    def create_snapshot(self, vm: VmRef, app_consistent: bool) -> SnapshotRef:
        spec = to_vmspec(self._v3(vm), self._v2(vm.id))
        cluster = (spec.extra.get("nutanix") or {}).get("cluster")
        if self.transport_for(cluster)[0] == "iscsi":
            return self._snapshot_iscsi(vm, spec, cluster, app_consistent)
        return self._snapshot_pd(vm, app_consistent)

    def _snapshot_iscsi(self, vm: VmRef, spec: VmSpec, cluster: str, app: bool) -> SnapshotRef:
        name = f"vmigrate-{self.ctx.run_id}"
        self.ctx.require_execute("nutanix v4 recovery point + volume group", vm=vm.name, name=name, app_consistent=app)
        v4 = self.v4
        portal = v4.data_services_ip(cluster)
        iqn = self._iscsi_factory(portal).initiator_name()
        data: dict[str, Any] = {"transport": "iscsi", "name": name, "portal": portal, "iqn": iqn, "cluster": cluster}
        ref = SnapshotRef(id=name, vm=vm, app_consistent=app, data=data)
        try:
            data["recovery_point"] = v4.create_recovery_point(vm.id, name, app_consistent=app)
            layout = v4.recovery_point_layout(data["recovery_point"])
            data["vm_recovery_point"] = layout["vm_recovery_point"]
            rp_disks = layout["disks"]
            unmapped = list(layout.get("unmapped") or [])
            if len(rp_disks) + len(unmapped) < len(spec.disks):
                raise SafetyGateError(f"recovery point covers {len(rp_disks) + len(unmapped)} disks, "
                                      f"the VM has {len(spec.disks)}")
            pairs, missing = [], []
            for d in spec.ordered_disks():
                keys = {d.id, d.source_handle.get("v3_uuid"), d.source_handle.get("device_uuid"),
                        d.source_handle.get("vmdisk_uuid")} - {None, ""}
                hit = {rp_disks[k] for k in keys if k in rp_disks}
                if len(hit) > 1:
                    raise SafetyGateError(f"recovery point has several copies of disk {d.address}: {sorted(hit)}")
                if hit:
                    pairs.append((d, hit.pop()))
                else:
                    missing.append(d)
            if missing and not unmapped:
                raise SafetyGateError(f"recovery point has no copy for disks {[d.address for d in missing]} "
                                      f"(it lists {sorted(rp_disks)})")
            # Volume-group LUNs: the identified copies in disk order, then (only if some disk is still
            # unidentified) every unlabelled copy. Prism may omit diskExtId and may include copies the
            # migration does not use (seen live: an ISO-backed CD-ROM), so unlabelled copies are
            # matched to source disks by the size Nutanix reports for each LUN.
            pairs.sort(key=lambda p: p[0].order)
            lun_drps = [drp for _, drp in pairs] + (unmapped if missing else [])
            data["volume_group"] = v4.create_volume_group(name, cluster)
            for i, drp in enumerate(lun_drps):
                v4.add_disk_from_recovery_point(data["volume_group"], data["recovery_point"], drp, i)
            vg_disks = v4.vg_disks(data["volume_group"])
            if len(vg_disks) != len(lun_drps):
                raise SafetyGateError(f"volume group has {len(vg_disks)} disks, {len(lun_drps)} were added")
            size_at = {g["index"]: g["size"] for g in vg_disks}
            lun_of = {drp: i for i, drp in enumerate(lun_drps)}
            for d, drp in pairs:
                if size_at.get(lun_of[drp]) not in (d.size_bytes, 0):
                    raise SafetyGateError(f"disk {d.address}: its recovery-point copy is {size_at.get(lun_of[drp])} "
                                          f"bytes, the source disk is {d.size_bytes}; refusing to copy")
            if missing:
                free = list(unmapped)
                trust = bool(self.config.get("rp_disk_order_trust", False))
                for d in missing:
                    cands = [drp for drp in free if size_at.get(lun_of[drp]) == d.size_bytes]
                    if len(cands) > 1 and not trust:
                        raise SafetyGateError(
                            f"Prism did not report which disk copy belongs to disk {d.address} (no diskExtId) and "
                            f"{len(cands)} copies have its size ({d.size_bytes} bytes), so it cannot be told apart. "
                            "Set rp_disk_order_trust: true on the Nutanix endpoint (advanced config) to accept "
                            "Prism's order.")
                    if not cands:
                        seen = sorted({size_at.get(lun_of[x]) for x in free})
                        raise SafetyGateError(
                            f"disk {d.address} ({d.size_bytes} bytes): no unlabelled recovery-point copy has that "
                            f"size (copies: {seen} bytes); refusing to guess")
                    pairs.append((d, cands[0]))
                    free.remove(cands[0])
                if free:
                    self.ctx.logger.info("recovery point holds %d extra disk copy(ies) not migrated (e.g. a CD-ROM "
                                         "with an ISO); sizes %s", len(free), [size_at.get(lun_of[x]) for x in free])
                self.ctx.logger.warning("recovery point lists no diskExtId; %d disk(s) matched to their copies by "
                                        "size", len(missing))
                pairs.sort(key=lambda p: p[0].order)
            data["disk_rps"] = {d.id: drp for d, drp in pairs}
            data["disk_map_by_size"] = bool(missing)
            data["vg_disks"] = vg_disks
            data["disk_index"] = {d.id: lun_of[drp] for d, drp in pairs}
            v4.attach_iscsi_client(data["volume_group"], iqn)
            data["attached"] = True
        except Exception:
            self.cleanup(ref)
            raise
        return ref

    def _devices(self, snapshot: SnapshotRef) -> dict[int, str]:
        d = snapshot.data
        sess = self._sessions.get(snapshot.id)
        if sess is None:
            sess = self._iscsi_factory(d["portal"])
            self._sessions[snapshot.id] = sess
            devs = sess.login(d["name"], expected=len(d["vg_disks"]))
            sess.devices = map_devices(d["vg_disks"], devs, self._size_of)   # type: ignore[attr-defined]
        return sess.devices  # type: ignore[attr-defined]

    def _snapshot_pd(self, vm: VmRef, app_consistent: bool) -> SnapshotRef:
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
        ref_data: dict[str, Any] = {"transport": "nfs", "protection_domain": self.pd, "snapshot_id": sid,
                                    "clone_name": clone_name,
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
        if snapshot.data.get("transport") == "iscsi":
            # LUNs actually visible on this worker that hold a VM disk's copy (a volume group may also
            # carry an unused copy, e.g. an ISO CD-ROM in the recovery point; it is not migrated)
            devs = self._devices(snapshot)
            return len({i for i in (snapshot.data.get("disk_index") or {}).values() if i in devs})
        return len(snapshot.data.get("disk_paths", {}))

    def open_disk(self, snapshot: SnapshotRef, disk: DiskSpec) -> DiskReader:
        if snapshot.data.get("transport") == "iscsi":
            if not str(snapshot.data.get("name", "")).startswith("vmigrate-"):
                raise LiveDiskReadRefused("nutanix iscsi: reads require a vmigrate volume group")
            idx = (snapshot.data.get("disk_index") or {}).get(disk.id)
            if idx is None:
                raise LiveDiskReadRefused(f"disk {disk.address} has no copy in volume group {snapshot.id}")
            path = self._devices(snapshot)[idx]
            size = self._size_of(path)
            if size < disk.size_bytes:
                raise SafetyGateError(f"{path}: {size} bytes < disk {disk.address} {disk.size_bytes}")
            return LunReader(path, size=disk.size_bytes, allocated=lambda: self._allocated(snapshot, disk))
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

    def _allocated(self, snapshot: SnapshotRef, disk: DiskSpec) -> list[ByteRange] | None:
        """Written regions of a disk in the recovery point (CRT with no reference), so a FULL copy reads only
        data the guest wrote instead of the whole disk (seen live: 10 TiB disks read in full at ~125 MB/s,
        ~23 h each, however little they held). None (= read everything) when CRT is off or fails."""
        d = snapshot.data
        drp = (d.get("disk_rps") or {}).get(disk.id)
        if not (self.supports_cbt() and self.config.get("crt_allocation", True) and drp and d.get("recovery_point")
                and d.get("vm_recovery_point")):
            return None
        cur = {"recovery_point": d["recovery_point"], "vm_recovery_point": d["vm_recovery_point"],
               "disk_recovery_point": drp}
        size = disk.size_bytes
        self.ctx.logger.info("disk %s: asking Prism which regions are written (allocation map)", disk.address)

        def progress(covered: int, n: int) -> None:
            self.ctx.logger.info("disk %s: allocation map %d%% (%d GiB of %d GiB scanned, %d regions)", disk.address,
                                 covered * 100 // max(size, 1), covered >> 30, size >> 30, n)
        try:
            regs, covered = self.v4.allocated_regions(
                cur, size, block_bytes=int(self.config.get("crt_block_bytes", 1 << 20)) or None,
                budget_s=float(self.config.get("crt_allocation_budget_s", 600)), progress=progress)
        except Exception as e:  # noqa: BLE001 - the copy still works, just reads the whole disk
            self.ctx.logger.warning("disk %s: allocation map not available (%s); reading the whole disk",
                                    disk.address, e)
            return None
        out = [ByteRange(o, min(n, size - o)) for o, n, t in regs if n > 0 and o < min(covered, size) and t != "ZEROED"]
        if covered < size:                     # map stopped early: everything not scanned is read
            out.append(ByteRange(covered, size - covered))
        out = normalize(out)
        used = sum(r.length for r in out)
        tail = "" if covered >= size else f", including {(size - covered) >> 30} GiB not scanned in time"
        self.ctx.logger.info("disk %s: reading %d GiB of %d GiB (allocation map%s)", disk.address,
                             used >> 30, size >> 30, tail)
        return out

    # ------------------------------------------------------------------ CRT (replica change tracking)
    def supports_cbt(self) -> bool:
        """CRT works on the iSCSI transport's v4 recovery points; any gap falls back to compare."""
        return bool(self.config.get("crt", True)) and self.config.get("transport", "auto") != "nfs"

    def keep_for_cbt(self) -> bool:
        return False      # only the recovery point is kept (retire_snapshot), not the VG / iSCSI session

    def checkpoint(self, snapshot: SnapshotRef) -> CheckpointRef:
        d = snapshot.data
        if d.get("transport") != "iscsi" or not d.get("disk_rps") or not d.get("vm_recovery_point"):
            return CheckpointRef(id=snapshot.id, data={})
        return CheckpointRef(id=d["recovery_point"], data={
            "recovery_point": d["recovery_point"], "vm_recovery_point": d["vm_recovery_point"],
            "disks": dict(d["disk_rps"]), "crt": True})

    def retire_snapshot(self, snapshot: SnapshotRef, previous: CheckpointRef | None) -> CheckpointRef:
        cp = self.checkpoint(snapshot)
        if not (self.supports_cbt() and cp.data.get("crt")):
            self.cleanup(snapshot)                          # recovery point deleted: nothing to reference
            return CheckpointRef(id=snapshot.id, data={})
        # tear down the read path but keep the recovery point as the next sync's CRT reference.
        # Say so explicitly: hiding its ID made the leftover sweep (find-by-name) delete it (seen live:
        # the second sync found no base and fell back to compare).
        keep = {**snapshot.data, "keep_recovery_point": True}
        self._cleanup_iscsi(SnapshotRef(id=snapshot.id, vm=snapshot.vm, app_consistent=snapshot.app_consistent,
                                        data=keep))
        try:
            self.ctx.require_execute("nutanix keep recovery point as CRT base", recovery_point=cp.id)
            self.v4.set_expiration(cp.id, float(self.config.get("crt_base_keep_hours", 24 * 14)))
        except Exception as e:  # noqa: BLE001 - never leave it half-kept; next sync falls back to compare
            self.ctx.logger.warning("could not keep recovery point %s for CRT (%s); deleting it", cp.id, e)
            self.v4.delete_recovery_point(cp.id)
            return CheckpointRef(id=snapshot.id, data={})
        if previous is not None and previous.data.get("crt") and previous.id != cp.id:
            self.release_checkpoint(previous)
        return cp

    def release_checkpoint(self, checkpoint: CheckpointRef) -> None:
        if checkpoint.data.get("crt") and checkpoint.data.get("recovery_point"):
            self.ctx.require_execute("nutanix delete CRT base recovery point", recovery_point=checkpoint.id)
            try:
                self.v4.delete_recovery_point(checkpoint.data["recovery_point"])
            except ProviderError as e:
                if "404" not in str(e):        # already expired / deleted is fine
                    raise

    def changed_regions(self, disk: DiskSpec, base: CheckpointRef, cur: CheckpointRef) -> list[ByteRange]:
        b, c = base.data, cur.data
        if not (b.get("crt") and c.get("crt")):
            raise ChangeTrackingUnavailable("no Nutanix recovery point recorded for CRT (first CRT sync, NFS "
                                            "transport, or the base could not be kept)")
        bd, cd = (b.get("disks") or {}).get(disk.id), (c.get("disks") or {}).get(disk.id)
        if not bd or not cd:
            raise ChangeTrackingUnavailable(f"disk {disk.address} is missing from a CRT recovery point")
        ref = {"recovery_point": b["recovery_point"], "vm_recovery_point": b["vm_recovery_point"],
               "disk_recovery_point": bd}
        now = {"recovery_point": c["recovery_point"], "vm_recovery_point": c["vm_recovery_point"],
               "disk_recovery_point": cd}
        try:
            regs = self.v4.changed_regions(now, ref, disk.size_bytes)
        except ProviderError as e:      # base expired/deleted, CRT unsupported on this AOS, ...
            raise ChangeTrackingUnavailable(f"Nutanix CRT failed for {disk.address}: {e}") from e
        return normalize([ByteRange(o, min(n, disk.size_bytes - o)) for o, n, _ in regs
                          if n > 0 and o < disk.size_bytes])

    # ------------------------------------------------------------------ cleanup
    def cleanup(self, snapshot: SnapshotRef) -> None:
        if snapshot.data.get("transport") == "iscsi":
            self._cleanup_iscsi(snapshot)
            return
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

    def _cleanup_iscsi(self, snapshot: SnapshotRef) -> None:
        d = snapshot.data
        self.ctx.require_execute("nutanix iscsi cleanup", volume_group=d.get("volume_group"),
                                 recovery_point="kept as CRT base" if d.get("keep_recovery_point")
                                 else d.get("recovery_point"))
        sess = self._sessions.pop(snapshot.id, None) or self._iscsi_factory(d.get("portal") or "127.0.0.1")
        sess.logout()
        sess.logout_matching(d["name"])            # also sessions left by a crashed worker process
        errors = []
        # an object Nutanix created whose ID we never learned (step failed mid-way): find it by name
        keep_rp = bool(d.get("keep_recovery_point"))
        for key, kind in (("volume_group", "volume-group"), ("recovery_point", "recovery-point")):
            if key == "recovery_point" and keep_rp:
                continue
            if not d.get(key) and d.get("name"):
                try:
                    found = self.v4.find_by_name(kind, d["name"])
                except Exception as e:  # noqa: BLE001 - keep cleaning; report at the end
                    errors.append(f"look up {kind} {d['name']}: {e}")
                    found = None
                if found:
                    self.ctx.logger.warning("cleaning up %s %s (%s) left without a recorded ID", kind, d["name"], found)
                    d[key] = found
        if d.get("volume_group"):
            if d.get("attached"):
                try:
                    self.v4.detach_iscsi_client(d["volume_group"], d["iqn"])
                except Exception as e:  # noqa: BLE001 - keep cleaning; report at the end
                    errors.append(f"detach: {e}")
            try:
                self.v4.delete_volume_group(d["volume_group"])
            except Exception as e:  # noqa: BLE001
                errors.append(f"delete volume group {d['volume_group']}: {e}")
        if d.get("recovery_point") and not keep_rp:
            try:
                self.v4.delete_recovery_point(d["recovery_point"])
            except Exception as e:  # noqa: BLE001
                errors.append(f"delete recovery point {d['recovery_point']}: {e}")
        if errors:
            raise ProviderError("nutanix iscsi cleanup incomplete (objects are named " + d["name"] + "): "
                                + "; ".join(errors))
