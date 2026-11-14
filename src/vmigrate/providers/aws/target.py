"""Purpose: AWS EC2 TargetProvider: each disk is written straight into an EBS snapshot with the EBS direct APIs,
the snapshots are registered as an AMI, and an instance is launched from it. No helper instance.

Usage (endpoint config; the same endpoint can be a source too. Keys are credentials):
    type: aws
    region: eu-west-1
    access_key_id / secret_access_key / role_arn / external_id     # as for the source
    network_map: {"VLAN-120": "subnet-0abc"}  # source network -> subnet (first NIC = primary)
    default_subnet: subnet-0abc               # for unmapped networks
    security_group_ids: [sg-0abc]             # optional (VPC default group otherwise)
    instance_type: m6i.xlarge                 # optional: else picked from m6i by vCPU / RAM
    volume_type: gp3
    kms_key_id: null                          # optional: encrypt the snapshots with this key
    key_name: null                            # optional EC2 key pair
    architecture: x86_64
    keep_ami: false                           # migrations: keep the AMI after the instance runs

How it works:
  write    : StartSnapshot (tags vmigrate:managed, vmigrate:disk=<name>) -> PutSnapshotBlock (512 KiB,
             SHA-256, parallel) -> CompleteSnapshot -> wait `completed` (aws/ebs_writer.py). Never-written
             blocks are zeros, so zero chunks are skipped. An interrupted copy restarts the disk.
  verify   : read back through the EBS direct read API (the source's EbsSnapshotReader)
  VM       : RegisterImage (root /dev/sda1 + /dev/sdf.., ENA, UEFI or legacy BIOS as the source) ->
             RunInstances (subnet, security groups, instance type, tags) -> the AMI is deregistered and the
             migration snapshots deleted once the instance runs (its volumes no longer need them)
  replica  : each sync writes a child snapshot of the previous one (ParentSnapshotId: only changes are put);
             the newest is tagged vmigrate:current. Restore points keep a snapshot (tag vmigrate:rp); a deploy
             registers an AMI from the chosen snapshots and leaves them in place.
  OSMorph  : the disks are never block-accessible here, so the guest must already have the drivers EC2 needs
             (Linux: nvme + ena in the initramfs; Windows: AWS NVMe + ENA drivers). See osmorph/cloud.py.
Safety: only resources tagged vmigrate:managed=true are deleted. Tested against fakes only.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...core import cancel
from ...core.context import RunContext
from ...core.errors import ConfigError, ProviderError, SafetyGateError
from ...core.interfaces import BootCheck, DiskHints, DiskReader, DiskWriter, TargetDiskRef, TargetProvider, VmRef
from ...ir import DiskBus, Firmware, VmSpec
from .ebs import EbsSnapshotReader
from .ebs_writer import EbsSnapshotWriter, complete_snapshot, start_snapshot

GIB = 1 << 30
MANAGED, DISK, CURRENT, RP = "vmigrate:managed", "vmigrate:disk", "vmigrate:current", "vmigrate:rp"
M6I = [(2, 8, "large"), (4, 16, "xlarge"), (8, 32, "2xlarge"), (16, 64, "4xlarge"), (32, 128, "8xlarge"),
       (48, 192, "12xlarge"), (64, 256, "16xlarge"), (96, 384, "24xlarge"), (128, 512, "32xlarge")]
DATA_DEVICES = [f"/dev/sd{c}" for c in "fghijklmnopqrstuvwxyz"] + [f"/dev/sd{a}{b}" for a in "ab" for b in "abcdefghijklmnopqrstuvwxyz"]
FAIL = ("Kernel panic", "dracut-initqueue timeout", "Entering emergency mode", "VFS: Unable to mount root",
        "INACCESSIBLE_BOOT_DEVICE", "No bootable device")


def disk_id(name: str) -> str:
    return "vmd-" + hashlib.sha1(name.encode()).hexdigest()[:10]  # noqa: S324 - a short stable label, not security


def tags(d: Mapping[str, Any]) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in d.get("Tags") or []}


def pick_instance_type(vcpus: int, ram_mib: int, family: str = "m6i") -> str:
    for c, gib, size in M6I:
        if c >= vcpus and gib * 1024 >= ram_mib:
            return f"{family}.{size}"
    raise ConfigError(f"aws: no {family} size has {vcpus} vCPUs and {ram_mib} MiB; set instance_type")


def image_request(spec: VmSpec, snaps: list[str], *, name: str, volume_type: str, arch: str) -> dict[str, Any]:
    if len(snaps) > 1 + len(DATA_DEVICES):
        raise ProviderError(f"aws: {len(snaps)} disks exceed the {1 + len(DATA_DEVICES)} device names used")
    bdm = [{"DeviceName": "/dev/sda1", "Ebs": {"SnapshotId": snaps[0], "VolumeType": volume_type,
                                                "DeleteOnTermination": True}}]
    bdm += [{"DeviceName": DATA_DEVICES[i], "Ebs": {"SnapshotId": s, "VolumeType": volume_type,
                                                     "DeleteOnTermination": True}} for i, s in enumerate(snaps[1:])]
    req: dict[str, Any] = {"Name": name[:128], "Description": f"vmigrate: {spec.name} from {spec.source_id}"[:255],
                           "Architecture": arch, "RootDeviceName": "/dev/sda1", "VirtualizationType": "hvm",
                           "EnaSupport": True, "BlockDeviceMappings": bdm,
                           "BootMode": "uefi" if spec.firmware == Firmware.UEFI else "legacy-bios"}
    if spec.firmware == Firmware.UEFI and spec.secure_boot:
        req["TpmSupport"] = "v2.0"
    return req


class AwsTarget(TargetProvider):
    type_name = "aws"
    platform_family = "aws"

    def __init__(self, name: str, config: Mapping[str, Any], ctx: RunContext, client: Any = None):
        super().__init__(name, config, ctx)
        self._client = client
        self._open: dict[str, tuple[str, EbsSnapshotWriter]] = {}
        self.poll = float(config.get("poll_interval", 10))
        self.op_timeout = float(config.get("op_timeout", 6 * 3600))

    @property
    def client(self) -> Any:
        if self._client is None:
            if not self.config.get("region"):
                raise ConfigError(f"aws target {self.name}: set region")
            from .client import AwsClient
            c = self.config
            self._client = AwsClient(region=c["region"], access_key_id=c.get("access_key_id"),
                                     secret_access_key=c.get("secret_access_key"), role_arn=c.get("role_arn"),
                                     external_id=c.get("external_id"))
        return self._client

    @property
    def ec2(self) -> Any:
        return self.client.ec2

    @property
    def ebs(self) -> Any:
        return self.client.ebs

    def _sleep(self, s: float) -> None:
        cancel.sleep(s)

    # ------------------------------------------------------------------ snapshots
    def _snapshots(self, name: str, *, states: tuple[str, ...] = ("completed",)) -> list[dict[str, Any]]:
        out = []
        for page in self.ec2.get_paginator("describe_snapshots").paginate(
                OwnerIds=["self"], Filters=[{"Name": f"tag:{DISK}", "Values": [name]},
                                            {"Name": f"tag:{MANAGED}", "Values": ["true"]}]):
            out += [s for s in page["Snapshots"] if s["State"] in states]
        return out

    def _current(self, name: str) -> dict[str, Any] | None:
        cur = [s for s in self._snapshots(name) if tags(s).get(CURRENT) == "true"]
        return max(cur, key=lambda s: s["StartTime"]) if cur else None

    def _wait_completed(self, sid: str) -> None:
        deadline = time.monotonic() + self.op_timeout
        while True:
            s = self.ec2.describe_snapshots(SnapshotIds=[sid])["Snapshots"][0]
            if s["State"] == "completed":
                return
            if s["State"] == "error":
                raise ProviderError(f"aws: snapshot {sid} failed: {s.get('StateMessage')}")
            if time.monotonic() > deadline:
                raise ProviderError(f"aws: snapshot {sid} still {s['State']} after {self.op_timeout}s")
            self._sleep(self.poll)

    def _delete_snapshot(self, s: Mapping[str, Any]) -> None:
        if tags(s).get(MANAGED) != "true":
            raise SafetyGateError(f"refusing to delete snapshot {s['SnapshotId']}: not tagged {MANAGED}")
        self.ec2.delete_snapshot(SnapshotId=s["SnapshotId"])

    # ------------------------------------------------------------------ checks + lists
    def check_connection(self) -> list[str]:
        try:
            self.client.identity()
            self.ec2.describe_subnets(MaxResults=5)
        except Exception as e:  # noqa: BLE001
            return [f"cannot use the AWS API in {self.config.get('region')}: {e}"]
        return []

    def preflight(self, spec: VmSpec | None = None) -> list[str]:
        p = self.check_connection()
        if spec is None or p:
            return p
        from ...osmorph.cloud import cloud_gate
        p += cloud_gate(spec, "aws")
        o = self._opts(spec)
        subnets = {s["SubnetId"] for s in self.ec2.describe_subnets()["Subnets"]}
        for nic in spec.nics:
            sn = (o.get("network_map") or {}).get(nic.network) or o.get("default_subnet")
            if not sn:
                p.append(f"no network_map entry (subnet) for source network {nic.network!r} and no default_subnet")
            elif sn not in subnets:
                p.append(f"subnet {sn!r} (for {nic.network!r}) not found in {o.get('region')}")
        if not spec.nics and not o.get("default_subnet"):
            p.append("set default_subnet: the VM has no NIC to map")
        return p

    def _opts(self, spec: VmSpec) -> dict[str, Any]:
        t = spec.extra.get("target_options") or {}
        return {**self.config, **t, "network_map": {**(self.config.get("network_map") or {}), **(t.get("network_map") or {})}}

    def list_networks(self) -> list[dict[str, Any]]:
        return sorted(({"id": s["SubnetId"], "name": f"{tags(s).get('Name') or s['SubnetId']} ({s['CidrBlock']}, "
                                                     f"{s['AvailabilityZone']})"}
                       for s in self.ec2.describe_subnets()["Subnets"]), key=lambda s: s["name"])

    def list_security_groups(self) -> list[dict[str, Any]]:
        return sorted(({"id": g["GroupId"], "name": f"{g['GroupName']} ({g['GroupId']})"}
                       for g in self.ec2.describe_security_groups()["SecurityGroups"]), key=lambda g: g["name"])

    def list_storage(self) -> list[dict[str, Any]]:
        return [{"id": t, "name": t} for t in ("gp3", "gp2", "io2", "io1", "st1", "sc1")]

    # ------------------------------------------------------------------ disks
    def create_disk(self, name: str, size_bytes: int, hints: DiskHints) -> TargetDiskRef:
        if hints.shared:
            raise ConfigError("aws target: shared (multi-attach) cluster disks are not supported")
        gib = max(1, -(-size_bytes // GIB))
        self.ctx.require_execute("aws reserve disk (written as an EBS snapshot)", name=name, size_gib=gib)
        return TargetDiskRef(id=disk_id(name), name=name, size_bytes=gib * GIB, data={"disk": name, "size_gib": gib})

    def find_disk(self, name: str) -> TargetDiskRef | None:
        cur = self._current(name)
        if not cur:
            return None
        return TargetDiskRef(id=disk_id(name), name=name, size_bytes=int(cur["VolumeSize"]) * GIB,
                             data={"disk": name, "size_gib": int(cur["VolumeSize"])})

    def open_disk_writer(self, disk: TargetDiskRef, *, resume: bool = False, incremental: bool = False) -> DiskWriter:
        name = disk.data["disk"]
        parent = self._current(name) if incremental else None
        if incremental and parent is None:
            raise ProviderError(f"aws: replica disk {name} has no completed snapshot to sync onto")
        tg = {MANAGED: "true", DISK: name, "vmigrate:run": self.ctx.run_id or ""}
        self.ctx.require_execute("aws start EBS snapshot", disk=name, size_gib=disk.data["size_gib"],
                                 parent=parent and parent["SnapshotId"])
        kw: dict[str, Any] = {"parent": parent["SnapshotId"] if parent else None,
                              "description": f"vmigrate disk {name}", "sleep": self._sleep}
        sid = start_snapshot(self.ebs, int(disk.data["size_gib"]), tg, **kw)
        pr = EbsSnapshotReader(self.ebs, parent["SnapshotId"], disk.size_bytes, sleep=self._sleep) if parent else None
        w = EbsSnapshotWriter(self.ebs, sid, disk.size_bytes, parent_reader=pr,
                              workers=int(self.config.get("write_workers", 16)), sleep=self._sleep)
        self._open[disk.id] = (sid, w)
        return w

    def commit_disk(self, disk: TargetDiskRef, writer_staged: Path | None) -> None:
        if disk.id not in self._open:
            raise ProviderError(f"aws: {disk.name}: no snapshot was written in this run")
        sid, w = self._open.pop(disk.id)
        w.close()
        self.ctx.require_execute("aws complete EBS snapshot", disk=disk.name, snapshot=sid, blocks=w.changed_blocks)
        complete_snapshot(self.ebs, sid, w.changed_blocks, sleep=self._sleep)
        self._wait_completed(sid)
        name = disk.data["disk"]
        for old in self._snapshots(name, states=("completed", "pending", "error")):
            if old["SnapshotId"] == sid:
                continue
            t = tags(old)
            if t.get(CURRENT) == "true":
                self.ec2.delete_tags(Resources=[old["SnapshotId"]], Tags=[{"Key": CURRENT}])
            if not t.get(RP):                         # not a restore point: superseded (or a dead attempt)
                self._delete_snapshot(old)
        self.ec2.create_tags(Resources=[sid], Tags=[{"Key": CURRENT, "Value": "true"}])

    def open_disk_reader(self, disk: TargetDiskRef) -> DiskReader | None:
        if not self.config.get("verify_after_commit", True):
            return None
        cur = self._current(disk.data["disk"])
        if not cur:
            return None
        return EbsSnapshotReader(self.ebs, disk.data.get("snapshot") or cur["SnapshotId"], disk.size_bytes,
                                 sleep=self._sleep)

    def morph_image(self, disk: TargetDiskRef) -> tuple[Path, str] | None:
        return None

    # ------------------------------------------------------------------ replica + restore points
    def supports_replica(self) -> bool:
        return True

    def supports_restore_points(self) -> tuple[bool, str]:
        return True, ""

    def snapshot_disk(self, disk: TargetDiskRef, name: str) -> str:
        cur = self._current(disk.data["disk"])
        if not cur:
            raise ProviderError(f"aws: {disk.name} has no completed snapshot")
        self.ec2.create_tags(Resources=[cur["SnapshotId"]], Tags=[{"Key": RP, "Value": name[:255]}])
        return str(cur["SnapshotId"])

    def delete_disk_snapshot(self, disk: TargetDiskRef, snapshot_id: str) -> None:
        got = self.ec2.describe_snapshots(Filters=[{"Name": "snapshot-id", "Values": [snapshot_id]}])["Snapshots"]
        if not got:
            return
        s = got[0]
        if tags(s).get(CURRENT) == "true":
            self.ec2.delete_tags(Resources=[snapshot_id], Tags=[{"Key": RP}])   # still the live replica
        else:
            self._delete_snapshot(s)

    def clone_disk(self, disk: TargetDiskRef, name: str, restore_point: str | None = None) -> TargetDiskRef:
        """Deploy: the AMI is registered straight from the replica's snapshot (or a restore point's)."""
        cur = self._current(disk.data["disk"])
        sid = restore_point or (cur and cur["SnapshotId"])
        if not sid:
            raise ProviderError(f"aws: {disk.name} has no completed snapshot")
        return TargetDiskRef(id=f"{disk.id}#{name}", name=name, size_bytes=disk.size_bytes,
                             data={**disk.data, "snapshot": sid, "clone": True})

    def delete_disk(self, disk: TargetDiskRef) -> None:
        if disk.data.get("clone"):
            return
        self.ctx.require_execute("aws delete disk snapshots", disk=disk.data["disk"])
        for s in self._snapshots(disk.data["disk"], states=("completed", "pending", "error")):
            self._delete_snapshot(s)

    # ------------------------------------------------------------------ VM
    def target_bus(self, spec: VmSpec) -> DiskBus:
        return DiskBus.NVME

    def _snap_for(self, d: TargetDiskRef) -> str:
        if d.data.get("snapshot"):
            return str(d.data["snapshot"])
        cur = self._current(d.data["disk"])
        if not cur:
            raise ProviderError(f"aws: {d.name} has no completed snapshot")
        return str(cur["SnapshotId"])

    def create_vm(self, spec: VmSpec, boot_disk: TargetDiskRef, data_disks: list[TargetDiskRef]) -> VmRef:
        disks = [boot_disk, *data_disks]
        o = self._opts(spec)
        snaps = [self._snap_for(d) for d in disks]
        run = (self.ctx.run_id or "run")[:12]
        req = image_request(spec, snaps, name=f"vmigrate-{spec.name}-{run}", volume_type=str(o.get("volume_type", "gp3")),
                            arch=str(o.get("architecture", "x86_64")))
        if o.get("kms_key_id"):
            for b in req["BlockDeviceMappings"]:
                b["Ebs"].update(Encrypted=True, KmsKeyId=o["kms_key_id"])
        self.ctx.require_execute("aws register AMI", name=req["Name"], disks=len(snaps))
        ami = self.ec2.register_image(**req, TagSpecifications=[
            {"ResourceType": "image", "Tags": [{"Key": MANAGED, "Value": "true"}]}])["ImageId"]
        self._wait_image(ami)
        subnets = [(o.get("network_map") or {}).get(n.network) or o.get("default_subnet") for n in spec.nics] \
            or [o.get("default_subnet")]
        if not all(subnets):
            raise ConfigError("aws: a source network has no subnet in network_map and there is no default_subnet")
        nics = [{"DeviceIndex": i, "SubnetId": sn, **({"Groups": list(o["security_group_ids"])}
                                                      if o.get("security_group_ids") else {})}
                for i, sn in enumerate(subnets)]
        itype = o.get("instance_type") or pick_instance_type(spec.cpu.total, spec.memory_bytes >> 20)
        tg = [{"Key": MANAGED, "Value": "true"}, {"Key": "Name", "Value": spec.name},
              {"Key": "vmigrate:disks", "Value": ",".join(d.id for d in disks)[:256]}]
        kw: dict[str, Any] = {"ImageId": ami, "InstanceType": itype, "MinCount": 1, "MaxCount": 1,
                              "NetworkInterfaces": nics,
                              "TagSpecifications": [{"ResourceType": "instance", "Tags": tg},
                                                    {"ResourceType": "volume", "Tags": tg[:2]}]}
        if o.get("key_name"):
            kw["KeyName"] = o["key_name"]
        self.ctx.require_execute("aws run instance", name=spec.name, type=itype, ami=ami)
        iid = self.ec2.run_instances(**kw)["Instances"][0]["InstanceId"]
        if not o.get("keep_ami", False):
            self.ec2.deregister_image(ImageId=ami)    # the instance's volumes no longer need it
            for d in disks:
                if not d.data.get("clone"):           # migration snapshots; a replica keeps its own
                    self.delete_disk(d)
        return VmRef(iid, spec.name, self.name)

    def _wait_image(self, ami: str) -> None:
        deadline = time.monotonic() + self.op_timeout
        while True:
            img = (self.ec2.describe_images(ImageIds=[ami])["Images"] or [{}])[0]
            if img.get("State") == "available":
                return
            if img.get("State") in ("failed", "invalid", "error"):
                raise ProviderError(f"aws: AMI {ami} {img.get('State')}: {img.get('StateReason')}")
            if time.monotonic() > deadline:
                raise ProviderError(f"aws: AMI {ami} not available after {self.op_timeout}s")
            self._sleep(self.poll)

    def _instance(self, iid: str) -> dict[str, Any] | None:
        r = self.ec2.describe_instances(Filters=[{"Name": "instance-id", "Values": [iid]}])["Reservations"]
        return r[0]["Instances"][0] if r and r[0]["Instances"] else None

    def find_vm(self, name: str) -> VmRef | None:
        r = self.ec2.describe_instances(Filters=[{"Name": "tag:Name", "Values": [name]},
                                                 {"Name": "instance-state-name",
                                                  "Values": ["pending", "running", "stopping", "stopped"]}])["Reservations"]
        hits = [i for x in r for i in x["Instances"]]
        if len(hits) > 1:
            raise SafetyGateError(f"aws: several instances named {name!r}")
        return VmRef(hits[0]["InstanceId"], name, self.name) if hits else None

    def attach_disk(self, vm: VmRef, disk: TargetDiskRef, order: int) -> None:
        raise ProviderError("aws: every disk is attached when the instance is launched from its AMI")

    def list_attached_disks(self, vm: VmRef) -> list[str]:
        inst = self._instance(vm.id) or {}
        devs = sorted(inst.get("BlockDeviceMappings") or [],
                      key=lambda b: (b["DeviceName"] != "/dev/sda1",
                                     DATA_DEVICES.index(b["DeviceName"]) if b["DeviceName"] in DATA_DEVICES else 999))
        ids = tags(inst).get("vmigrate:disks", "").split(",")
        if ids and ids != [""] and len(ids) == len(devs):
            return ids
        return [b["Ebs"]["VolumeId"] for b in devs]

    def check_boot(self, vm: VmRef, timeout: float = 900, *, guest_os: str | None = None,
                   progress: Any = None) -> BootCheck:
        """Both EC2 status checks `ok` = booted and reachable by AWS; a known failure in the console log fails."""
        say = progress or self.ctx.logger.info
        start, last = time.monotonic(), -1e9
        while time.monotonic() - start < timeout:
            inst = self._instance(vm.id) or {}
            state = (inst.get("State") or {}).get("Name", "")
            if state in ("stopped", "terminated", "shutting-down"):
                return BootCheck(False, f"instance is {state}")
            st = self.ec2.describe_instance_status(InstanceIds=[vm.id])["InstanceStatuses"]
            if st and st[0]["SystemStatus"]["Status"] == "ok" and st[0]["InstanceStatus"]["Status"] == "ok":
                return BootCheck(True, "EC2 system and instance status checks passed")
            if st and st[0]["InstanceStatus"]["Status"] == "impaired":
                log = self._console(vm.id)
                return BootCheck(False, "EC2 instance status check failed (guest unreachable: drivers? network?)", log)
            log = self._console(vm.id)
            for pat in FAIL:
                if pat in log:
                    return BootCheck(False, f"guest boot failure: {pat!r} in the console", log)
            el = time.monotonic() - start
            if el - last >= 60:
                last = el
                say(f"boot check: instance {state or '?'}, {int(el)}s of {int(timeout)}s; waiting for the EC2 status checks")
            self._sleep(self.poll)
        return BootCheck(False, f"EC2 status checks did not pass within {int(timeout)}s")

    def _console(self, iid: str) -> str:
        import base64
        try:
            out = self.ec2.get_console_output(InstanceId=iid, Latest=True).get("Output") or ""
            return base64.b64decode(out).decode(errors="replace") if out and not out.isprintable() else out
        except Exception:  # noqa: BLE001 - console output is a hint only
            return ""

    # ------------------------------------------------------------------ cleanup
    def cleanup_on_failure(self, created: Mapping[str, Any]) -> None:
        self.ctx.require_execute("aws cleanup created resources")
        for _, (sid, w) in list(self._open.items()):
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass
        self._open.clear()
        vm = created.get("target_vm")
        if vm:
            inst = self._instance(vm["id"])
            if inst is None or (inst.get("State") or {}).get("Name") == "terminated":
                self.ctx.logger.info("cleanup: instance %s is already gone", vm["id"])
            elif tags(inst).get(MANAGED) != "true":
                raise SafetyGateError(f"refusing to terminate {vm['id']}: not tagged {MANAGED}")
            else:
                self.ec2.terminate_instances(InstanceIds=[vm["id"]])
        for d in created.get("target_disks", []):
            data = d.get("data") or {}
            if data.get("disk") and not data.get("clone"):
                self.delete_disk(TargetDiskRef(d["id"], d["name"], d["size_bytes"], data=data))
