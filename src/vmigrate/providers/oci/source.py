"""Purpose: Oracle Cloud Infrastructure SourceProvider. OCI has no API that reads volume blocks over
the network, so the worker must be an OCI instance in the source region: each volume backup is
restored to a temporary volume, attached read-only to the worker, and read as a block device.

Usage (endpoint config; the API key goes in the encrypted credentials):
    type: oci
    region: eu-frankfurt-1
    tenancy_ocid: ocid1.tenancy.oc1..aaa
    user_ocid: ocid1.user.oc1..aaa
    fingerprint: 12:34:...
    api_key_pem: "-----BEGIN PRIVATE KEY-----..."        # credential
    compartment_ocid: ocid1.compartment.oc1..aaa          # where the source instances are
    worker_instance_ocid: ocid1.instance.oc1..aaa         # default: this worker, from instance metadata
    allow_per_disk_snapshots: false

How it works:
  inspect  : instance + boot/block volume attachments + volumes + image + VNICs -> IR
  gate     : attachments listed by instance vs the compartment-wide attachment list
  refuse   : a RUNNING instance with more than one volume unless allow_per_disk_snapshots (each
             volume backup is its own point in time); app_consistent (OCI backups do not quiesce)
  snapshot : one FULL boot-volume / volume backup per volume, tagged vmigrate-run (the point in time
             is when each backup is requested; the copy happens in the background)
  read     : backup restored to a temporary volume in the worker's availability domain, attached
             paravirtualized + read-only at a consistent device path (/dev/oracleoci/oraclevdX),
             read O_RDONLY; detached and deleted when the last reader closes
  replicas : no changed-block API, so syncs use compare mode
  cleanup  : temporary volumes and backups, only those tagged vmigrate-run
Volumes the guest attached itself over iSCSI need their iscsiadm records and _netdev mounts cleaned
up after migration (listed in the IR as extra.oci.iscsi_volumes).
NOT run against a live tenancy yet: tests use a fake API and fake block devices.
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
from .api import TAG


class OciSource(SourceProvider):
    type_name = "oci"

    def __init__(self, name: str, config: Mapping[str, Any], ctx: RunContext, api: Any = None,
                 worker: dict[str, Any] | None = None, dev_timeout: float = 180):
        super().__init__(name, config, ctx)
        for k in ("region", "compartment_ocid"):
            if not config.get(k):
                raise ConfigError(f"oci source {name}: missing {k!r}")
        self._api = api
        self._worker = worker
        self.dev_timeout = dev_timeout
        self._vm: dict[str, dict[str, Any]] = {}
        self._open: dict[str, list[Any]] = {}
        self._lock = threading.Lock()

    @property
    def api(self) -> Any:
        if self._api is None:
            from .api import OciApi
            c = self.config
            self._api = OciApi(region=c["region"], tenancy=c.get("tenancy_ocid", ""), user=c.get("user_ocid", ""),
                               fingerprint=c.get("fingerprint", ""), key_pem=c.get("api_key_pem", ""))
        return self._api

    def worker(self) -> dict[str, Any]:
        if self._worker is None:
            wid = self.config.get("worker_instance_ocid")
            if wid:
                self._worker = self.api.instance(wid)
            else:
                from .api import worker_identity
                self._worker = worker_identity()
        return self._worker

    @property
    def compartment(self) -> str:
        return str(self.config["compartment_ocid"])

    # ------------------------------------------------------------------ inventory
    def _load(self, vm: VmRef) -> dict[str, Any]:
        inst = self.api.instance(vm.id)
        boots = self.api.boot_attachments(inst["availabilityDomain"], self.compartment, vm.id)
        vols = self.api.volume_attachments(self.compartment, vm.id)
        volumes = {b["bootVolumeId"]: self.api.volume(b["bootVolumeId"], True) for b in boots}
        volumes.update({a["volumeId"]: self.api.volume(a["volumeId"], False) for a in vols})
        image = self.api.image((inst.get("sourceDetails") or {}).get("imageId") or inst.get("imageId") or "")
        vnics = self.api.vnics(self.compartment, vm.id)
        d = {"inst": inst, "boots": boots, "vols": vols, "volumes": volumes, "image": image, "vnics": vnics}
        self._vm[vm.id] = d
        return d

    def _spec(self, d: dict[str, Any]) -> VmSpec:
        try:
            return mapping.to_vmspec(d["inst"], d["boots"], d["vols"], d["volumes"], d["image"], d["vnics"])
        except mapping.OciMappingError as e:
            raise SafetyGateError(str(e)) from e

    def check_connection(self) -> list[str]:
        try:
            self.api.instances(self.compartment)
        except Exception as e:  # noqa: BLE001
            return [f"cannot list OCI instances in {self.compartment} ({self.config['region']}): {e}"]
        return []

    def preflight(self, spec: VmSpec | None = None) -> list[str]:
        try:
            self.worker()
        except Exception as e:  # noqa: BLE001
            return [str(e)]
        return []

    def list_vms(self) -> list[VmRef]:
        return [VmRef(i["id"], i.get("displayName") or i["id"], self.name) for i in self.api.instances(self.compartment)]

    def find_vm(self, name_or_id: str) -> VmRef:
        insts = self.api.instances(self.compartment)
        hit = [i for i in insts if i.get("id") == name_or_id] or [i for i in insts if i.get("displayName") == name_or_id]
        if not hit:
            raise ProviderError(f"oci: no instance {name_or_id!r} in compartment {self.compartment}")
        if len(hit) > 1:
            raise SafetyGateError(f"oci: {len(hit)} instances are named {name_or_id!r}; use the OCID")
        return VmRef(hit[0]["id"], hit[0].get("displayName") or hit[0]["id"], self.name)

    def inspect_vm(self, vm: VmRef) -> VmSpec:
        return self._spec(self._load(vm))

    def disk_count_crosscheck(self, vm: VmRef) -> dict[str, int]:
        d = self._vm.get(vm.id) or self._load(vm)
        return mapping.crosscheck(d["boots"], d["vols"], self.api.volume_attachments(self.compartment), vm.id)

    def shutdown_vm(self, vm: VmRef, *, force: bool = False, timeout: int = 900) -> str:
        if self.api.instance(vm.id).get("lifecycleState") == "STOPPED":
            return f"{vm.name} already stopped"
        self.ctx.require_execute("oci instance STOP" if force else "oci instance SOFTSTOP", instance=vm.id)
        self.api.stop(vm.id, force=force)
        return f"{vm.name} stopped"

    # ------------------------------------------------------------------ snapshots (backups)
    def create_snapshot(self, vm: VmRef, app_consistent: bool) -> SnapshotRef:
        if app_consistent:
            raise SafetyGateError("oci: volume backups are crash-consistent (no guest quiesce). Set app_consistent: "
                                  "false, or stop the instance first.")
        d = self._load(vm)
        spec = self._spec(d)
        state = d["inst"].get("lifecycleState")
        if len(spec.disks) > 1 and state != "STOPPED" and not self.config.get("allow_per_disk_snapshots"):
            raise SafetyGateError(f"oci: {vm.name} is {state} with {len(spec.disks)} volumes; each volume backup is its "
                                  "own point in time. Stop it first, or set allow_per_disk_snapshots: true.")
        tag = f"vmigrate-{self.ctx.run_id}"
        self.ctx.require_execute("oci volume backups", instance=vm.id, volumes=len(spec.disks))
        backups: dict[str, dict[str, Any]] = {}
        ref = SnapshotRef(id=tag, vm=vm, app_consistent=False, data={"backups": backups, "tag": tag})
        try:
            for disk in spec.ordered_disks():           # requested back to back: each is a point in time
                boot = bool(disk.source_handle["boot"])
                bid = self.api.backup_volume(disk.id, boot, f"{tag}-{disk.order:02d}", {TAG: tag})
                backups[disk.id] = {"id": bid, "boot": boot}
            for b in backups.values():
                self.api.wait_state(self.api.backup_path(b["id"], b["boot"]), "AVAILABLE")
        except Exception:
            self.cleanup(ref)
            raise
        return ref

    def snapshot_disk_count(self, snapshot: SnapshotRef) -> int | None:
        return len(snapshot.data.get("backups") or {})

    def open_disk(self, snapshot: SnapshotRef, disk: DiskSpec) -> DiskReader:
        b = (snapshot.data.get("backups") or {}).get(disk.id)
        if not b or not snapshot.id.startswith("vmigrate-"):
            raise LiveDiskReadRefused(f"oci: no vmigrate backup for {disk.label}")
        key = b["id"]
        with self._lock:
            ent = self._open.get(key)
            if ent is None:
                ent = self._open[key] = [*self._attach(b, snapshot.data["tag"], disk), 0]
            ent[-1] += 1
        r = SharedView(self, key, ent[0])
        if r.size < disk.size_bytes:
            r.close()
            raise SafetyGateError(f"{disk.label}: attached size {r.size} < inspected {disk.size_bytes}")
        return r

    def _attach(self, b: dict[str, Any], tag: str, disk: DiskSpec) -> tuple[RawFileReader, str, str, bool]:
        w = self.worker()
        name = f"{tag}-{disk.order:02d}-read"
        self.ctx.require_execute("oci restore + attach to worker", backup=b["id"], worker=w["id"])
        vid = self.api.restore(b["id"], b["boot"], w["availabilityDomain"], w.get("compartmentId") or self.compartment,
                               name, {TAG: tag})
        aid = None
        try:
            dev = self.api.free_device(w["id"])
            aid = self.api.attach(w["id"], vid, dev, name)
            deadline = time.monotonic() + self.dev_timeout
            while not os.path.exists(dev):
                if time.monotonic() > deadline:
                    raise ProviderError(f"volume {vid} attached but {dev} did not appear")
                time.sleep(1)
            return RawFileReader(dev, size=block_size(dev)), vid, aid, b["boot"]
        except Exception:
            if aid:
                self.api.detach(aid)
            self.api.delete_volume(vid, b["boot"])
            raise

    def _teardown(self, ent: list[Any]) -> None:
        reader, vid, aid, boot = ent[0], ent[1], ent[2], ent[3]
        reader.close()
        try:
            self.api.detach(aid)
        finally:
            self.api.delete_volume(vid, boot)

    def _release(self, key: str) -> None:
        with self._lock:
            ent = self._open.get(key)
            if ent is None:
                return
            ent[-1] -= 1
            if ent[-1] > 0:
                return
            self._open.pop(key)
        self._teardown(ent)

    def cleanup(self, snapshot: SnapshotRef) -> None:
        self.ctx.require_execute("oci delete backups", tag=snapshot.id)
        for b in (snapshot.data.get("backups") or {}).values():
            with self._lock:
                ent = self._open.pop(b["id"], None)
            if ent is not None:
                self._teardown(ent)
            self.api.delete_backup(b["id"], b["boot"])

    def supports_cbt(self) -> bool:
        return False
