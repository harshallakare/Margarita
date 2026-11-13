"""Purpose: Google Cloud Compute Engine SourceProvider. Google has no API that reads snapshot blocks
over the network, so (like Google's own Migrate tooling) the worker must be a Compute Engine VM:
each snapshot becomes a temporary disk attached to the worker read-only and read as a block device.

Usage (endpoint config; the key goes in the encrypted credentials):
    type: gcp
    project: prod-123                        # project of the source VMs
    service_account_key: '{"type": "service_account", ...}'   # credential; empty = the worker VM's account
    worker_instance: margarita-worker-1      # default: this worker, from the metadata server
    worker_zone: europe-west1-b              # default: this worker's zone
    temp_disk_type: pd-balanced              # type of the temporary disks
    allow_per_disk_snapshots: false
    ignore_local_ssd: false

How it works:
  inspect  : aggregated instances + each disk + machine type -> IR (virtio-scsi / NVMe disks,
             virtio-net / gVNIC NICs)
  gate     : instance.disks vs the zone's disks whose users include the instance
  refuse   : local SSDs (unless ignore_local_ssd), regional disks, and a RUNNING instance with more
             than one disk unless allow_per_disk_snapshots (each disk is snapshotted separately)
  snapshot : one standard snapshot per disk (labels vmigrate-run=<tag>); app_consistent sends
             guestFlush=true (VSS on Windows, the guest environment's flush on Linux)
  read     : temporary disk from the snapshot in the worker's zone (snapshots are global, so the
             worker may be in another zone or region), attached READ_ONLY to the worker as
             /dev/disk/by-id/google-<device>, read O_RDONLY; detached and deleted when the last
             reader closes
  replicas : Compute Engine exposes no changed-block API, so syncs use compare mode
  cleanup  : temporary disks and snapshots, only those labelled vmigrate-run
Costs: snapshot storage, temporary disks while a copy runs, and egress when the target is outside
Google Cloud. NOT run against a live project yet: tests use a fake API and fake block devices.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Mapping
from typing import Any

from ...core.context import RunContext
from ...core.errors import ConfigError, LiveDiskReadRefused, ProviderError, SafetyGateError
from ...core.fileio import RawFileReader
from ...core.interfaces import DiskReader, SnapshotRef, SourceProvider, VmRef
from ...ir import DiskSpec, VmSpec
from ..common.attached import SharedView, block_size
from . import mapping
from .api import LABEL


class GcpSource(SourceProvider):
    type_name = "gcp"

    def __init__(self, name: str, config: Mapping[str, Any], ctx: RunContext, api: Any = None,
                 worker: tuple[str, str] | None = None, by_id_dir: str = "/dev/disk/by-id", dev_timeout: float = 180):
        super().__init__(name, config, ctx)
        if not config.get("project"):
            raise ConfigError(f"gcp source {name}: missing 'project'")
        self._api = api
        self._worker = worker
        self.by_id_dir, self.dev_timeout = by_id_dir, dev_timeout
        self._vm: dict[str, dict[str, Any]] = {}
        self._open: dict[str, list[Any]] = {}       # temp disk name -> [RawFileReader, refcount, zone, worker, device]
        self._lock = threading.Lock()

    @property
    def api(self) -> Any:
        if self._api is None:
            from .api import GceApi
            self._api = GceApi(project=self.config["project"], key_json=self.config.get("service_account_key"))
        return self._api

    def worker(self) -> tuple[str, str]:
        """(zone, instance) of the worker VM the temporary disks attach to."""
        if self._worker is None:
            z, i = self.config.get("worker_zone"), self.config.get("worker_instance")
            if not (z and i):
                from .api import worker_identity
                _, mz, mi = worker_identity()
                z, i = z or mz, i or mi
            self._worker = (str(z), str(i))
        return self._worker

    # ------------------------------------------------------------------ inventory
    def _split(self, vm_id: str) -> tuple[str, str]:
        zone, _, name = vm_id.partition("/")
        if not name:
            raise ProviderError(f"gcp: VM id {vm_id!r} is not <zone>/<name>")
        return zone, name

    def _load(self, vm: VmRef) -> dict[str, Any]:
        zone, name = self._split(vm.id)
        inst = self.api.instance(zone, name)
        disks = {}
        for a in mapping.persistent(inst):
            src = a.get("source", "")
            if "/zones/" in src:
                disks[src] = self.api.disk(mapping.last(src.split("/disks/")[0]), mapping.last(src))
        mt = self.api.machine_type(zone, mapping.last(inst.get("machineType", "")))
        d = {"inst": inst, "disks": disks, "mt": mt, "zone": zone}
        self._vm[vm.id] = d
        return d

    def _spec(self, d: dict[str, Any]) -> VmSpec:
        try:
            return mapping.to_vmspec(d["inst"], d["disks"], d["mt"], ignore_local_ssd=bool(self.config.get("ignore_local_ssd")))
        except mapping.GcpMappingError as e:
            raise SafetyGateError(str(e)) from e

    def check_connection(self) -> list[str]:
        try:
            self.api.instances()
        except Exception as e:  # noqa: BLE001
            return [f"cannot list Compute Engine instances in project {self.config['project']}: {e}"]
        return []

    def preflight(self, spec: VmSpec | None = None) -> list[str]:
        p = []
        try:
            self.api.token()
        except Exception as e:  # noqa: BLE001
            return [f"cannot sign in to Google Cloud: {e}"]
        try:
            self.worker()
        except Exception as e:  # noqa: BLE001
            p.append(str(e))
        if not os.path.isdir(self.by_id_dir):
            p.append(f"{self.by_id_dir} not found: the worker must be a Linux Compute Engine VM")
        return p

    def _ref(self, i: dict[str, Any]) -> VmRef:
        return VmRef(f"{mapping.zone_of(i)}/{i['name']}", i["name"], self.name)

    def list_vms(self) -> list[VmRef]:
        return [self._ref(i) for i in self.api.instances()]

    def find_vm(self, name_or_id: str) -> VmRef:
        insts = self.api.instances()
        hit = [i for i in insts if f"{mapping.zone_of(i)}/{i['name']}" == name_or_id or str(i.get("id")) == name_or_id] \
            or [i for i in insts if i.get("name") == name_or_id]
        if not hit:
            raise ProviderError(f"gcp: no instance {name_or_id!r} in project {self.config['project']}")
        if len(hit) > 1:
            raise SafetyGateError(f"gcp: {len(hit)} instances are named {name_or_id!r}; use <zone>/<name> ("
                                  + ", ".join(f"{mapping.zone_of(i)}/{i['name']}" for i in hit) + ")")
        return self._ref(hit[0])

    def inspect_vm(self, vm: VmRef) -> VmSpec:
        return self._spec(self._load(vm))

    def disk_count_crosscheck(self, vm: VmRef) -> dict[str, int]:
        d = self._vm.get(vm.id) or self._load(vm)
        return mapping.crosscheck(d["inst"], self.api.disks_used_by(d["zone"], d["inst"]["selfLink"]))

    def shutdown_vm(self, vm: VmRef, *, force: bool = False, timeout: int = 900) -> str:
        zone, name = self._split(vm.id)
        if self.api.instance(zone, name).get("status") in ("TERMINATED", "STOPPED"):
            return f"{vm.name} already stopped"
        self.ctx.require_execute("gce instances.stop", instance=vm.id)
        self.api.stop(zone, name)       # GCE always runs the guest's shutdown first (up to its grace period)
        return f"{vm.name} stopped"

    # ------------------------------------------------------------------ snapshots
    def create_snapshot(self, vm: VmRef, app_consistent: bool) -> SnapshotRef:
        d = self._load(vm)
        spec = self._spec(d)
        status = d["inst"].get("status")
        if len(spec.disks) > 1 and status not in ("TERMINATED", "STOPPED") and not self.config.get("allow_per_disk_snapshots"):
            raise SafetyGateError(f"gcp: {vm.name} is {status} with {len(spec.disks)} disks. Each disk is snapshotted "
                                  "separately, so they would be from slightly different moments. Stop it first, or set "
                                  "allow_per_disk_snapshots: true on the endpoint.")
        tag = f"vmigrate-{self.ctx.run_id}".lower()[:63]
        self.ctx.require_execute("gce createSnapshot", instance=vm.id, disks=len(spec.disks), guest_flush=app_consistent)
        snaps: dict[str, str] = {}
        ref = SnapshotRef(id=tag, vm=vm, app_consistent=app_consistent, data={"snapshots": snaps, "tag": tag})
        try:
            ops = []
            for disk in spec.ordered_disks():          # started back to back to keep the skew small
                sname = f"{tag}-{disk.order:02d}"[:63]
                ops.append(self.api.create_snapshot(disk.source_handle["zone"], disk.source_handle["name"], sname,
                                                    {LABEL: tag}, guest_flush=app_consistent))
                snaps[disk.id] = sname
            for op in ops:
                self.api.wait(op)
            for sname in snaps.values():
                s = self.api.snapshot(sname) or {}
                if s.get("status") != "READY":
                    raise ProviderError(f"gcp: snapshot {sname} is {s.get('status')}, not READY")
        except Exception:
            self.cleanup(ref)
            raise
        return ref

    def snapshot_disk_count(self, snapshot: SnapshotRef) -> int | None:
        return len(snapshot.data.get("snapshots") or {})

    def open_disk(self, snapshot: SnapshotRef, disk: DiskSpec) -> DiskReader:
        sname = (snapshot.data.get("snapshots") or {}).get(disk.id)
        if not sname or not snapshot.id.startswith("vmigrate-"):
            raise LiveDiskReadRefused(f"gcp: no vmigrate snapshot for {disk.label}")
        key = f"{sname}-r"[:63]
        with self._lock:
            ent = self._open.get(key)
            if ent is None:
                ent = self._open[key] = [self._attach(key, sname, snapshot.data["tag"]), 0]
            ent[1] += 1
        r = SharedView(self, key, ent[0])
        if r.size < disk.size_bytes:
            r.close()
            raise SafetyGateError(f"{disk.label}: attached size {r.size} < inspected {disk.size_bytes}")
        return r

    def _attach(self, name: str, sname: str, tag: str) -> RawFileReader:
        zone, worker = self.worker()
        self.ctx.require_execute("gce temporary disk + attachDisk", disk=name, worker=worker, zone=zone)
        self.api.disk_from_snapshot(zone, name, sname, {LABEL: tag}, self.config.get("temp_disk_type") or "pd-balanced")
        try:
            self.api.attach(zone, worker, name, name)
            dev = f"{self.by_id_dir}/google-{name}"
            deadline = time.monotonic() + self.dev_timeout
            while not os.path.exists(dev):
                if time.monotonic() > deadline:
                    raise ProviderError(f"disk {name} attached but {dev} did not appear")
                time.sleep(1)
            return RawFileReader(dev, size=block_size(dev))
        except Exception:
            self._detach_delete(name)
            raise

    def _detach_delete(self, name: str) -> None:
        zone, worker = self.worker()
        try:
            self.api.detach(zone, worker, name)
        finally:
            self.api.delete_disk(zone, name)

    def _release(self, key: str) -> None:
        with self._lock:
            ent = self._open.get(key)
            if ent is None:
                return
            ent[1] -= 1
            if ent[1] > 0:
                return
            self._open.pop(key)
        ent[0].close()
        self._detach_delete(key)

    def cleanup(self, snapshot: SnapshotRef) -> None:
        self.ctx.require_execute("gce delete snapshots", tag=snapshot.id)
        for sname in (snapshot.data.get("snapshots") or {}).values():
            key = f"{sname}-r"[:63]
            with self._lock:
                ent = self._open.pop(key, None)
            if ent is not None:
                ent[0].close()
                self._detach_delete(key)
            self.api.delete_snapshot(sname)

    def supports_cbt(self) -> bool:
        return False
