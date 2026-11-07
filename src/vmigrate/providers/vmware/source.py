"""Purpose: VMware vSphere SourceProvider (vCenter or a standalone ESXi host).

Usage (endpoint config; password goes in the encrypted credentials):
    type: vmware
    host: vcenter.example.com          # vCenter (recommended) or ESXi
    username: migrate@vsphere.local
    password: ...
    verify_tls: true
    transport: auto                    # vddk | http | auto (vddk when nbdkit + VDDK are installed)
    vddk_libdir: /opt/vmware-vix-disklib-distrib
    vddk_transports: nbdssl:nbd        # add hotadd only when the worker is a VM on the same vSphere
    thumbprint: AA:BB:...              # optional pin; else taken from the (verified) TLS certificate
    nbdkit: nbdkit

How it works:
  inspect  : vm.config devices -> IR (mapping.py); controller MODEL recorded (pvscsi, lsilogic, ...)
  gate     : device-list disk count vs layoutEx.disk count; snapshot device count after snapshot
  refuse   : independent disks (not in snapshots = live reads), physical RDMs, unknown controllers
  snapshot : CreateSnapshot_Task named vmigrate-<run>, memory off, quiesce = app_consistent
  read     : vddk  -> nbdkit-vddk (vm=moref, snapshot=moref, file=<base disk at snapshot>) over NBD
             http  -> Range GETs of the base -flat.vmdk, only if the snapshot's disk has no parent
  CBT      : when the VM has changeTrackingEnabled, replica syncs read only QueryChangedDiskAreas
             ranges; checkpoints are per-disk changeIds, so no old snapshot is kept on the source.
             A first full copy also uses changeId "*" to skip never-written regions.
  cleanup  : RemoveSnapshot_Task only for snapshots named vmigrate-* that this run recorded
The source VM is never reconfigured, powered off or deleted. CBT is not enabled by this tool —
enable it yourself (VM > Advanced > ctkEnabled) if you want incremental replica reads.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping
from typing import Any

from ...core.context import RunContext
from ...core.errors import ChangeTrackingUnavailable, ConfigError, LiveDiskReadRefused, ProviderError, SafetyGateError
from ...core.interfaces import CheckpointRef, DiskReader, SnapshotRef, SourceProvider, VmRef
from ...core.ranges import ByteRange
from ...ir import DiskSpec, VmSpec
from . import mapping
from .client import SNAP_PREFIX, server_thumbprint
from .transports import HttpFlatReader, VddkExport, vddk_problems


class _AllocatedReader(DiskReader):
    """Wraps a reader and exposes allocated ranges from CBT changeId '*' (full-copy speed-up)."""

    def __init__(self, inner: DiskReader, ranges: list[ByteRange] | None):
        self.inner, self._ranges = inner, ranges

    @property
    def size(self) -> int:
        return self.inner.size

    def read(self, offset: int, length: int) -> bytes:
        return self.inner.read(offset, length)

    def allocated_ranges(self) -> list[ByteRange] | None:
        return self._ranges

    def close(self) -> None:
        self.inner.close()


class VmwareSource(SourceProvider):
    type_name = "vmware"

    def __init__(self, name: str, config: Mapping[str, Any], ctx: RunContext, client: Any = None):
        super().__init__(name, config, ctx)
        for k in ("host", "username"):
            if not config.get(k):
                raise ConfigError(f"vmware source {name}: missing {k!r}")
        self.host = config["host"]
        self.port = int(config.get("port", 443))
        self.verify_tls = bool(config.get("verify_tls", True))
        self.transport = config.get("transport", "auto")
        if self.transport not in ("auto", "vddk", "http"):
            raise ConfigError("vmware transport must be auto, vddk or http")
        self.nbdkit = config.get("nbdkit", "nbdkit")
        self.libdir = config.get("vddk_libdir")
        self._client = client
        self._info: dict[str, dict[str, Any]] = {}

    @property
    def client(self) -> Any:
        if self._client is None:
            from .client import VsphereClient
            self._client = VsphereClient(self.host, self.config["username"], self.config.get("password", ""),
                                         port=self.port, verify_tls=self.verify_tls)
        return self._client

    def close(self) -> None:
        if self._client is not None and hasattr(self._client, "close"):
            self._client.close()

    # ------------------------------------------------------------------ helpers
    def _use_vddk(self) -> bool:
        if self.transport == "vddk":
            return True
        if self.transport == "http":
            return False
        return not vddk_problems(self.nbdkit, self.libdir)

    def _thumbprint(self) -> str:
        tp = self.config.get("thumbprint")
        if tp:
            return tp
        if not self.verify_tls:
            raise ConfigError("vmware: set `thumbprint` when verify_tls is false (VDDK must not trust an "
                              "unverified certificate)")
        return server_thumbprint(self.host, self.port)

    def info(self, vm: VmRef, refresh: bool = False) -> dict[str, Any]:
        if refresh or vm.id not in self._info:
            self._info[vm.id] = self.client.vm_info(vm.id)
        return self._info[vm.id]

    # ------------------------------------------------------------------ contract
    def check_connection(self) -> list[str]:
        try:
            self.client.about()
            self.client.list_vms()
        except Exception as e:  # noqa: BLE001
            return [f"cannot log in to vSphere {self.host}: {e}"]
        return []

    def preflight(self, spec: VmSpec | None = None) -> list[str]:
        p: list[str] = []
        try:
            self.client.about()
        except Exception as e:  # noqa: BLE001
            return [f"cannot reach vSphere {self.host}: {e}"]
        if self.transport == "vddk":
            p += vddk_problems(self.nbdkit, self.libdir)
        if self.transport == "auto" and vddk_problems(self.nbdkit, self.libdir):
            self.ctx.logger.warning("vmware %s: VDDK not available here; using the HTTPS flat-file transport, "
                                    "which refuses disks with existing snapshots and non-flat backings", self.name)
        return p

    def list_vms(self) -> list[VmRef]:
        return [VmRef(v["moref"], v["name"], self.name) for v in self.client.list_vms()]

    def find_vm(self, name_or_id: str) -> VmRef:
        vms = self.client.list_vms()
        hit = [v for v in vms if v["moref"] == name_or_id] or [v for v in vms if v["name"] == name_or_id]
        if not hit:
            raise ProviderError(f"vmware: no VM {name_or_id!r}")
        if len(hit) > 1:
            raise SafetyGateError(f"vmware: {len(hit)} VMs are named {name_or_id!r}; use the moref "
                                  f"({', '.join(v['moref'] for v in hit)})")
        return VmRef(hit[0]["moref"], hit[0]["name"], self.name)

    def inspect_vm(self, vm: VmRef) -> VmSpec:
        try:
            return mapping.to_vmspec(self.info(vm, refresh=True))
        except mapping.VmwareMappingError as e:
            raise SafetyGateError(str(e)) from e

    def disk_count_crosscheck(self, vm: VmRef) -> dict[str, int]:
        return mapping.crosscheck(self.info(vm))

    def create_snapshot(self, vm: VmRef, app_consistent: bool) -> SnapshotRef:
        name = f"{SNAP_PREFIX}{self.ctx.run_id}"
        self.ctx.require_execute("vsphere create snapshot", vm=vm.name, name=name, quiesce=app_consistent)
        smoref = self.client.create_snapshot(vm.id, name, quiesce=app_consistent)
        disks = {str(d["key"]): d for d in self.client.snapshot_disks(vm.id, smoref)}
        return SnapshotRef(id=smoref, vm=vm, app_consistent=app_consistent,
                           data={"name": name, "vm_moref": vm.id, "disks": disks,
                                 "datacenter": self.info(vm).get("datacenter")})

    def snapshot_disk_count(self, snapshot: SnapshotRef) -> int | None:
        return len(snapshot.data.get("disks") or {})

    def _snap_disk(self, snapshot: SnapshotRef, disk: DiskSpec) -> dict[str, Any]:
        sd = (snapshot.data.get("disks") or {}).get(str(disk.source_handle["key"]))
        if sd is None:
            raise SafetyGateError(f"disk key {disk.source_handle['key']} missing from snapshot {snapshot.id}")
        return sd

    def open_disk(self, snapshot: SnapshotRef, disk: DiskSpec) -> DiskReader:
        if not snapshot.id or not str(snapshot.data.get("name", "")).startswith(SNAP_PREFIX):
            raise LiveDiskReadRefused("vmware: reads require a vmigrate snapshot")
        sd = self._snap_disk(snapshot, disk)
        vm_moref = snapshot.data["vm_moref"]
        if self._use_vddk():
            inner: DiskReader = VddkExport(
                nbdkit=self.nbdkit, libdir=self.libdir or "", server=self.host, user=self.config["username"],
                password=self.config.get("password", ""), thumbprint=self._thumbprint(), vm_moref=vm_moref,
                snapshot_moref=snapshot.id, file_name=sd["file_name"],
                transports=self.config.get("vddk_transports", "nbdssl:nbd"))
        else:
            ok, why = mapping.flat_readable(sd)
            if not ok:
                raise SafetyGateError(f"vmware http transport cannot read {sd['file_name']}: {why}")
            ds, rel = mapping.flat_base_path(sd["file_name"])
            q = urllib.parse.urlencode({"dcPath": snapshot.data.get("datacenter") or "", "dsName": ds})
            url = f"https://{self.host}:{self.port}/folder/{urllib.parse.quote(rel)}?{q}"
            inner = HttpFlatReader(self._http_get(), url, disk.size_bytes)
        if inner.size < disk.size_bytes:
            inner.close()
            raise SafetyGateError(f"disk {sd['file_name']}: readable size {inner.size} < capacity {disk.size_bytes}")
        alloc = None
        if self.info(VmRef(vm_moref, "", self.name)).get("cbt_enabled"):
            try:
                alloc = self.changed_ranges_raw(vm_moref, snapshot.id, disk, "*")
            except ChangeTrackingUnavailable:
                alloc = None
        return _AllocatedReader(inner, alloc)

    def _http_get(self) -> Any:
        import requests  # provider-private dependency
        s = requests.Session()
        s.verify = self.verify_tls
        s.headers["Cookie"] = self.client.session_cookie()
        return lambda url, headers: s.get(url, headers=headers, timeout=300)

    # ------------------------------------------------------------------ CBT
    def supports_cbt(self) -> bool:
        return any(i.get("cbt_enabled") for i in self._info.values())

    def keep_for_cbt(self) -> bool:
        return False   # change ids survive snapshot removal; no base snapshot stays on the VM

    def checkpoint(self, snapshot: SnapshotRef) -> CheckpointRef:
        ids = {k: d.get("change_id") for k, d in (snapshot.data.get("disks") or {}).items()}
        return CheckpointRef(id=snapshot.id, data={"change_ids": ids, "snapshot": snapshot.id,
                                                   "vm_moref": snapshot.data.get("vm_moref")})

    def changed_ranges_raw(self, vm_moref: str, snap_moref: str, disk: DiskSpec, change_id: str) -> list[ByteRange]:
        key = int(disk.source_handle["key"])
        return mapping.changed_ranges(lambda start: self.client.query_changed(vm_moref, snap_moref, key, start, change_id),
                                      disk.size_bytes)

    def changed_regions(self, disk: DiskSpec, base: CheckpointRef, cur: CheckpointRef) -> list[ByteRange]:
        cid = (base.data.get("change_ids") or {}).get(str(disk.source_handle["key"]))
        if not cid:
            raise ChangeTrackingUnavailable(f"no CBT change id recorded for {disk.label or disk.id} at the last sync")
        return self.changed_ranges_raw(cur.data["vm_moref"], cur.id, disk, cid)

    def cleanup(self, snapshot: SnapshotRef) -> None:
        if not str(snapshot.data.get("name", "")).startswith(SNAP_PREFIX):
            raise SafetyGateError(f"refusing to remove snapshot {snapshot.id}: not created by vmigrate")
        self.ctx.require_execute("vsphere remove snapshot", vm=snapshot.vm.name, snapshot=snapshot.id)
        self.client.remove_snapshot(snapshot.data["vm_moref"], snapshot.id)
