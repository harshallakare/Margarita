"""Purpose: Azure SourceProvider (VMs on managed disks), reading incremental snapshots over a
read-only SAS; replica syncs read only the pages changed since the previous snapshot.

Usage (endpoint config; the client secret goes in the encrypted credentials):
    type: azure
    subscription_id: 00000000-...           # required
    auth: client_secret                     # or managed_identity (worker is an Azure VM)
    tenant_id: 00000000-...                 # client_secret auth
    client_id: 00000000-...                 # client_secret auth: app registration's application id
    client_secret: ...                      # credential
    managed_identity_client_id: ...         # optional: a user-assigned identity
    cloud: public                           # public | usgov | china
    resource_group: rg-prod                 # optional: only list VMs in this resource group
    snapshot_resource_group: rg-migration   # optional: where snapshots go (default: the VM's group)
    allow_per_disk_snapshots: false         # running multi-disk VMs: see "consistency"
    read_workers: 16                        # parallel 4 MiB range reads per disk
    sas_hours: 8                            # lifetime of each read SAS (renewed if it runs out)

How it works:
  inspect  : ARM VM ($expand=instanceView) + each managed disk + NICs + VM size -> IR
  gate     : storageProfile disk count vs the subscription's disks whose managedBy is this VM
  refuse   : unmanaged VHD disks, ephemeral OS disks, Confidential VMs, app_consistent (Azure has
             no crash-to-app quiesce through this API), and see consistency below
  snapshot : one INCREMENTAL snapshot per managed disk (tagged vmigrate:run=vmigrate-<run>), all
             started back to back, then waited until Succeeded and 100% copied
  read     : beginGetAccess -> read-only SAS URL of the snapshot (a page blob); parallel 4 MiB range
             reads with Content-MD5 checks; never-written pages are skipped on a fresh copy. The SAS
             lives only in memory and access is ended when the last reader closes.
  CBT      : Get Page Ranges with x-ms-previous-snapshot-url (previous incremental snapshot of the
             same disk) -> changed + cleared pages. The previous snapshot set is kept as the base
             and replaced each sync.
  cleanup  : endGetAccess + delete, only for snapshots tagged vmigrate:run

Consistency: Azure snapshots are taken per disk, so a RUNNING VM with more than one disk gets disks
from slightly different moments. That is refused unless the VM is stopped/deallocated, or you set
allow_per_disk_snapshots: true (fine when the other disks hold data that tolerates it, or for
replica syncs before a cutover that stops the VM). A single-disk VM is always crash-consistent.

Costs: snapshot storage (incremental: changed pages only) and snapshot reads leave the Azure region
as egress, billed by Azure.

NOT run against a live Azure subscription yet: the tests use a fake ARM and a fake page blob.
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
from .blob import SasSnapshotReader, page_ranges
from .client import power_state, split_id


class AzureSource(SourceProvider):
    type_name = "azure"

    def __init__(self, name: str, config: Mapping[str, Any], ctx: RunContext, client: Any = None):
        super().__init__(name, config, ctx)
        if not config.get("subscription_id"):
            raise ConfigError(f"azure source {name}: missing 'subscription_id'")
        self._client = client
        self._vm: dict[str, dict[str, Any]] = {}
        self._grants: dict[str, list[Any]] = {}      # snapshot id -> [sas, open readers]; memory only
        self._glock = threading.RLock()

    @property
    def client(self) -> Any:
        if self._client is None:
            from .client import AzureClient
            c = self.config
            self._client = AzureClient(
                subscription_id=c["subscription_id"], tenant_id=c.get("tenant_id"), client_id=c.get("client_id"),
                client_secret=c.get("client_secret"), auth=c.get("auth") or "client_secret",
                cloud=c.get("cloud") or "public", managed_identity_client_id=c.get("managed_identity_client_id"))
        return self._client

    # ------------------------------------------------------------------ inventory
    def _load(self, vm: VmRef) -> dict[str, Any]:
        v = self.client.vm(vm.id)
        disks = {r["id"]: self.client.disk(r["id"]) for r in mapping.disk_refs(v) if r["id"]}
        nics = [self.client.nic(n["id"]) for n in
                ((v.get("properties") or {}).get("networkProfile") or {}).get("networkInterfaces") or []]
        size = self.client.vm_size(v["location"], (v.get("properties") or {})["hardwareProfile"]["vmSize"])
        d = {"vm": v, "disks": disks, "nics": nics, "size": size}
        self._vm[vm.id] = d
        return d

    def _spec(self, d: dict[str, Any]) -> VmSpec:
        try:
            return mapping.to_vmspec(d["vm"], d["disks"], d["nics"], d["size"])
        except mapping.AzureMappingError as e:
            raise SafetyGateError(str(e)) from e

    def _ref(self, v: dict[str, Any]) -> VmRef:
        return VmRef(v["id"], v.get("name") or v["id"], self.name)

    def check_connection(self) -> list[str]:
        try:
            self.client.subscription()
            self.client.vms(self.config.get("resource_group"))   # proves Microsoft.Compute/virtualMachines/read
        except Exception as e:  # noqa: BLE001
            return [f"cannot use Azure subscription {self.config['subscription_id']} with these credentials: {e}"]
        return []

    def preflight(self, spec: VmSpec | None = None) -> list[str]:
        try:
            self.client.subscription()
        except Exception as e:  # noqa: BLE001
            return [f"cannot sign in to Azure: {e}"]
        return []

    def list_vms(self) -> list[VmRef]:
        """Names are unique per resource group only; a name used in two groups is listed as
        "<resource group>/<name>" so the wizard can pick it (find_vm accepts that form)."""
        vms = self.client.vms(self.config.get("resource_group"))
        seen: dict[str, int] = {}
        for v in vms:
            seen[str(v.get("name")).lower()] = seen.get(str(v.get("name")).lower(), 0) + 1
        return [VmRef(v["id"], v.get("name") if seen[str(v.get("name")).lower()] == 1
                      else f"{split_id(v['id']).get('resourcegroups')}/{v.get('name')}", self.name) for v in vms]

    def find_vm(self, name_or_id: str) -> VmRef:
        """A full resource id, "<resource group>/<name>", or a name that is unique in the scope."""
        vms = self.client.vms(self.config.get("resource_group"))
        key = name_or_id.strip().lower()
        hit = [v for v in vms if v["id"].lower() == key]
        if not hit and "/" in key and not key.startswith("/"):
            rg, nm = key.split("/", 1)
            hit = [v for v in vms if split_id(v["id"]).get("resourcegroups", "").lower() == rg
                   and str(v.get("name")).lower() == nm]
        if not hit:
            hit = [v for v in vms if str(v.get("name")).lower() == key]
        if not hit:
            raise ProviderError(f"azure: no VM {name_or_id!r} in subscription {self.config['subscription_id']}"
                                + (f" / resource group {self.config['resource_group']}" if self.config.get("resource_group") else ""))
        if len(hit) > 1:
            raise SafetyGateError(f"azure: {len(hit)} VMs are named {name_or_id!r}; use <resource group>/<name> ("
                                  + ", ".join(f"{split_id(v['id']).get('resourcegroups')}/{v.get('name')}" for v in hit) + ")")
        return self._ref(hit[0])

    def inspect_vm(self, vm: VmRef) -> VmSpec:
        return self._spec(self._load(vm))

    def disk_count_crosscheck(self, vm: VmRef) -> dict[str, int]:
        d = self._vm.get(vm.id) or self._load(vm)
        return mapping.crosscheck(d["vm"], self.client.disks_managed_by(vm.id))

    def shutdown_vm(self, vm: VmRef, *, force: bool = False, timeout: int = 900) -> str:
        self.ctx.require_execute("azure VM powerOff", vm=vm.id, force=force)
        return f"{vm.name} {self.client.power_off(vm.id, force=force)}"

    # ------------------------------------------------------------------ snapshots
    def create_snapshot(self, vm: VmRef, app_consistent: bool) -> SnapshotRef:
        if app_consistent:
            raise SafetyGateError("azure: managed-disk snapshots are crash-consistent; there is no guest quiesce "
                                  "through this API. Set app_consistent: false, or stop the VM first for a fully "
                                  "consistent copy.")
        d = self._load(vm)                    # fresh: power state and disks as of now
        spec = self._spec(d)
        state = power_state(d["vm"])
        if len(spec.disks) > 1 and state not in ("stopped", "deallocated") \
                and not self.config.get("allow_per_disk_snapshots"):
            raise SafetyGateError(
                f"azure: {vm.name} is {state} with {len(spec.disks)} disks. Azure snapshots each disk separately, "
                "so the disks would be from slightly different moments. Stop the VM first, or set "
                "allow_per_disk_snapshots: true on the endpoint if that is acceptable for this VM.")
        run_tag = f"vmigrate-{self.ctx.run_id}"
        rg = self.config.get("snapshot_resource_group") or split_id(vm.id).get("resourcegroups")
        self.ctx.require_execute("azure incremental snapshots", vm=vm.id, resource_group=rg, tag=run_tag)
        snaps: dict[str, str] = {}
        ref = SnapshotRef(id=run_tag, vm=vm, app_consistent=False,
                          data={"vm_id": vm.id, "run_tag": run_tag, "snapshots": snaps, "power_state": state})
        try:
            for disk in spec.ordered_disks():         # started back to back to keep the skew small
                name = f"{run_tag}-{disk.order:02d}"[:80]
                snaps[disk.id] = self.client.start_snapshot(rg, name, d["vm"]["location"], disk.id, run_tag)
            for sid in snaps.values():
                self.client.wait_snapshot(sid)
        except Exception:
            self.cleanup(ref)
            raise
        return ref

    def snapshot_disk_count(self, snapshot: SnapshotRef) -> int | None:
        return len(snapshot.data.get("snapshots") or {})

    def _sid(self, snapshot: SnapshotRef | CheckpointRef, disk: DiskSpec) -> str | None:
        snaps = snapshot.data.get("snapshots") or {}
        return snaps.get(disk.id) or next((v for k, v in snaps.items() if k.lower() == disk.id.lower()), None)

    # SAS grants are reference-counted: copy and verify may read the same snapshot at once
    def _acquire(self, sid: str) -> str:
        with self._glock:
            g = self._grants.get(sid)
            if g is None:
                g = self._grants[sid] = [self.client.grant_access(sid, self._sas_seconds()), 0]
            g[1] += 1
            return g[0]

    def _sas(self, sid: str, renew: bool) -> str:
        with self._glock:
            g = self._grants.get(sid)
            if g is None:
                raise ProviderError(f"azure: no access grant for {sid}")
            if renew:
                self.ctx.logger.info("azure: read SAS for %s expired or was refused; granting a new one",
                                     sid.rsplit("/", 1)[-1])
                g[0] = self.client.grant_access(sid, self._sas_seconds())
            return g[0]

    def _release(self, sid: str) -> None:
        with self._glock:
            g = self._grants.get(sid)
            if g is None:
                return
            g[1] -= 1
            if g[1] > 0:
                return
            self._grants.pop(sid, None)
        try:
            self.client.revoke_access(sid)
        except Exception as e:  # noqa: BLE001 - the SAS expires anyway; cleanup revokes again
            self.ctx.logger.warning("azure: could not end access to %s: %s", sid.rsplit("/", 1)[-1], e)

    def _sas_seconds(self) -> int:
        return int(float(self.config.get("sas_hours") or 8) * 3600)

    def open_disk(self, snapshot: SnapshotRef, disk: DiskSpec) -> DiskReader:
        sid = self._sid(snapshot, disk)
        if not sid or not snapshot.id.startswith("vmigrate-"):
            raise LiveDiskReadRefused(f"azure: no vmigrate snapshot for disk {disk.label} ({disk.id})")
        self._acquire(sid)
        try:
            return SasSnapshotReader(lambda renew: self._sas(sid, renew), disk.size_bytes, http=self.client.http,
                                     workers=int(self.config.get("read_workers", 16)),
                                     on_close=lambda: self._release(sid))
        except Exception:
            self._release(sid)
            raise

    def cleanup(self, snapshot: SnapshotRef) -> None:
        self.ctx.require_execute("azure delete snapshots", run_tag=snapshot.id)
        for sid in list((snapshot.data.get("snapshots") or {}).values()):
            with self._glock:
                self._grants.pop(sid, None)
            self.client.delete_snapshot(sid)

    # ------------------------------------------------------------------ CBT (page-range diff)
    def supports_cbt(self) -> bool:
        return True

    def keep_for_cbt(self) -> bool:
        return True     # the diff needs the previous incremental snapshot to still exist

    def checkpoint(self, snapshot: SnapshotRef) -> CheckpointRef:
        return CheckpointRef(id=snapshot.id, data={"snapshots": dict(snapshot.data.get("snapshots") or {})})

    def changed_regions(self, disk: DiskSpec, base: CheckpointRef, cur: CheckpointRef) -> list[ByteRange]:
        a, b = self._sid(base, disk), self._sid(cur, disk)
        if not a or not b:
            raise ChangeTrackingUnavailable(f"{disk.label}: no snapshot pair for the page-range diff")
        try:
            base_sas = self._acquire(a)
        except ProviderError as e:
            raise ChangeTrackingUnavailable(f"{disk.label}: previous snapshot unavailable ({e})") from e
        try:
            self._acquire(b)
            try:
                return page_ranges(self.client.http, lambda renew: self._sas(b, renew), disk.size_bytes,
                                   previous=base_sas)
            finally:
                self._release(b)
        except ProviderError as e:
            raise ChangeTrackingUnavailable(f"page-range diff {a.rsplit('/', 1)[-1]} -> {b.rsplit('/', 1)[-1]}: "
                                            f"{e}") from e
        finally:
            self._release(a)
