"""Purpose: AWS EC2 SourceProvider (EBS-backed instances), reading snapshots via the EBS direct APIs.

Usage (endpoint config; keys go in the encrypted credentials):
    type: aws
    region: eu-west-1
    access_key_id: AKIA...                # credential (or leave empty to use the worker's instance role)
    secret_access_key: ...                # credential
    role_arn: arn:aws:iam::123:role/vmigrate-source     # optional: assume a role in the source account
    external_id: ...                      # optional, with role_arn
    ignore_instance_store: false          # true only if instance-store disks hold scratch data
    read_workers: 16                      # parallel GetSnapshotBlock calls per disk

How it works:
  inspect  : DescribeInstances + DescribeInstanceTypes + DescribeVolumes -> IR (Nitro: nvme/ena;
             Xen: xen-blkfront/xen-netfront -> OSMorph injects virtio for KVM targets)
  gate     : BlockDeviceMappings EBS count vs DescribeVolumes(attachment.instance-id), plus the
             snapshot set must cover exactly the inspected volumes
  refuse   : instance-store data (unless ignore_instance_store), app_consistent (EBS multi-volume
             snapshots are crash-consistent; there is no native Linux quiesce), non-unique Name tags
  snapshot : CreateSnapshots for the whole instance: one crash-consistent point in time across all
             volumes, tagged vmigrate:run=vmigrate-<run>, waited until completed
  read     : EBS direct APIs (ListSnapshotBlocks window + parallel GetSnapshotBlock, SHA-256
             verified); never-written blocks are skipped on a fresh copy. No helper instance.
  CBT      : ListChangedBlocks(previous snapshot, current snapshot) per volume; the previous
             snapshot set is kept as the diff base and rotated each sync.
  cleanup  : DeleteSnapshot, only for snapshots tagged vmigrate:run that this run created
Costs: EBS direct API requests and snapshot storage are billed by AWS; the kept base snapshot of a
replica is incremental (only changed blocks are stored).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...core.context import RunContext
from ...core.errors import ChangeTrackingUnavailable, ConfigError, LiveDiskReadRefused, ProviderError, SafetyGateError
from ...core.interfaces import CheckpointRef, DiskReader, SnapshotRef, SourceProvider, VmRef
from ...core.ranges import ByteRange
from ...ir import DiskSpec, VmSpec
from . import mapping
from .ebs import EbsSnapshotReader, changed_block_indices


class AwsSource(SourceProvider):
    type_name = "aws"

    def __init__(self, name: str, config: Mapping[str, Any], ctx: RunContext, client: Any = None):
        super().__init__(name, config, ctx)
        if not config.get("region"):
            raise ConfigError(f"aws source {name}: missing 'region'")
        self._client = client
        self._vm: dict[str, dict[str, Any]] = {}

    @property
    def client(self) -> Any:
        if self._client is None:
            from .client import AwsClient
            c = self.config
            self._client = AwsClient(region=c["region"], access_key_id=c.get("access_key_id"),
                                     secret_access_key=c.get("secret_access_key"), session_token=c.get("session_token"),
                                     role_arn=c.get("role_arn"), external_id=c.get("external_id"),
                                     endpoint_url=c.get("endpoint_url"))
        return self._client

    def _load(self, vm: VmRef) -> dict[str, Any]:
        inst = self.client.instance(vm.id)
        d = {"inst": inst, "vols": self.client.volumes_for(vm.id), "itype": self.client.instance_type(inst["InstanceType"])}
        self._vm[vm.id] = d
        return d

    def _spec(self, d: dict[str, Any]) -> VmSpec:
        try:
            return mapping.to_vmspec(d["inst"], d["vols"], d["itype"],
                                     ignore_instance_store=bool(self.config.get("ignore_instance_store")))
        except mapping.AwsMappingError as e:
            raise SafetyGateError(str(e)) from e

    # ------------------------------------------------------------------ contract
    def check_connection(self) -> list[str]:
        try:
            self.client.identity()
            self.client.instances()   # proves ec2:DescribeInstances in the configured region
        except Exception as e:  # noqa: BLE001
            return [f"cannot use AWS in {self.config['region']} with these credentials: {e}"]
        return []

    def preflight(self, spec: VmSpec | None = None) -> list[str]:
        try:
            self.client.identity()
        except Exception as e:  # noqa: BLE001
            return [f"cannot authenticate to AWS ({self.config['region']}): {e}"]
        return []

    def list_vms(self) -> list[VmRef]:
        return [VmRef(i["InstanceId"], mapping.tag(i, "Name") or i["InstanceId"], self.name) for i in self.client.instances()]

    def find_vm(self, name_or_id: str) -> VmRef:
        insts = self.client.instances()
        hit = [i for i in insts if i["InstanceId"] == name_or_id] or \
              [i for i in insts if mapping.tag(i, "Name") == name_or_id]
        if not hit:
            raise ProviderError(f"aws: no instance {name_or_id!r} in {self.config['region']}")
        if len(hit) > 1:
            raise SafetyGateError(f"aws: {len(hit)} instances are named {name_or_id!r}; use the instance id "
                                  f"({', '.join(i['InstanceId'] for i in hit)})")
        return VmRef(hit[0]["InstanceId"], mapping.tag(hit[0], "Name") or hit[0]["InstanceId"], self.name)

    def inspect_vm(self, vm: VmRef) -> VmSpec:
        return self._spec(self._load(vm))

    def disk_count_crosscheck(self, vm: VmRef) -> dict[str, int]:
        d = self._vm.get(vm.id) or self._load(vm)
        return mapping.crosscheck(d["inst"], d["vols"])

    def shutdown_vm(self, vm: VmRef, *, force: bool = False, timeout: int = 900) -> str:
        self.ctx.require_execute("ec2 StopInstances", instance=vm.id, force=force)
        return f"{vm.name} {self.client.stop_instance(vm.id, force=force, timeout=timeout)}"

    def create_snapshot(self, vm: VmRef, app_consistent: bool) -> SnapshotRef:
        if app_consistent:
            raise SafetyGateError("aws: EBS multi-volume snapshots are crash-consistent (there is no native quiesce "
                                  "for Linux; Windows VSS needs SSM). Set app_consistent: false, or stop the "
                                  "instance first for a fully consistent copy.")
        d = self._vm.get(vm.id) or self._load(vm)
        spec = self._spec(d)
        run_tag = f"vmigrate-{self.ctx.run_id}"
        self.ctx.require_execute("ec2 CreateSnapshots", instance=vm.id, tag=run_tag)
        snaps = self.client.create_snapshots(vm.id, run_tag)
        ref = SnapshotRef(id=run_tag, vm=vm, app_consistent=False,
                          data={"instance_id": vm.id, "run_tag": run_tag, "snapshots": snaps})
        want = {x.id for x in spec.disks}
        if set(snaps) != want:
            self.cleanup(ref)
            raise SafetyGateError(f"snapshot set covers {sorted(snaps)} but the instance was inspected with "
                                  f"{sorted(want)} (volumes attached/detached meanwhile?)")
        return ref

    def snapshot_disk_count(self, snapshot: SnapshotRef) -> int | None:
        return len(snapshot.data.get("snapshots") or {})

    def open_disk(self, snapshot: SnapshotRef, disk: DiskSpec) -> DiskReader:
        sid = (snapshot.data.get("snapshots") or {}).get(disk.id)
        if not sid or not snapshot.id.startswith("vmigrate-"):
            raise LiveDiskReadRefused(f"aws: no vmigrate snapshot for volume {disk.id}")
        return EbsSnapshotReader(self.client.ebs, sid, disk.size_bytes,
                                 workers=int(self.config.get("read_workers", 16)))

    def cleanup(self, snapshot: SnapshotRef) -> None:
        self.ctx.require_execute("ec2 DeleteSnapshot", run_tag=snapshot.id)
        for sid in (snapshot.data.get("snapshots") or {}).values():
            self.client.delete_snapshot(sid)

    # ------------------------------------------------------------------ CBT (ListChangedBlocks)
    def supports_cbt(self) -> bool:
        return True

    def keep_for_cbt(self) -> bool:
        return True     # ListChangedBlocks needs the previous snapshot to still exist

    def checkpoint(self, snapshot: SnapshotRef) -> CheckpointRef:
        return CheckpointRef(id=snapshot.id, data={"snapshots": dict(snapshot.data.get("snapshots") or {})})

    def changed_regions(self, disk: DiskSpec, base: CheckpointRef, cur: CheckpointRef) -> list[ByteRange]:
        a = (base.data.get("snapshots") or {}).get(disk.id)
        b = (cur.data.get("snapshots") or {}).get(disk.id)
        if not a or not b:
            raise ChangeTrackingUnavailable(f"{disk.label}: no snapshot pair for ListChangedBlocks")
        try:
            idx, block = changed_block_indices(self.client.ebs, a, b)
        except ProviderError as e:
            raise ChangeTrackingUnavailable(f"ListChangedBlocks {a} -> {b}: {e}") from e
        return mapping.blocks_to_ranges(idx, block, disk.size_bytes)
