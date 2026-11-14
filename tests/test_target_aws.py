"""Purpose: AWS target: disks written into EBS snapshots through a fake EBS direct API (blocks, parents,
checksums), partial-block handling, AMI + instance, replica child snapshots and restore points, the cloud
OSMorph gate, cleanup safety.

Usage: pytest tests/test_target_aws.py
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
from datetime import datetime, timedelta, timezone

import pytest

from vmigrate.config.loader import EngineConfig, MigrationConfig
from vmigrate.core.context import RunContext
from vmigrate.core.errors import SafetyGateError
from vmigrate.core.interfaces import DiskHints, VmRef
from vmigrate.engine import MigrationRunner, Phase, StateStore
from vmigrate.ir import CpuSpec, DiskBus, DiskSpec, Firmware, GuestOS, NicSpec, OsFamily, VmSpec
from vmigrate.providers import TARGETS
from vmigrate.providers.aws import target as T
from vmigrate.providers.aws.ebs_writer import BLOCK, EbsSnapshotWriter
from vmigrate.providers.fake.provider import FakeSource

from .conftest import fake_vm

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


class Pager:
    def __init__(self, fn):
        self.fn = fn

    def paginate(self, **kw):
        yield self.fn(**kw)


class FakeAws:
    """EC2 + EBS direct, just enough for the target."""

    def __init__(self):
        self.snaps: dict[str, dict] = {}           # id -> {State, VolumeSize, Tags, parent, blocks, StartTime}
        self.images: dict[str, dict] = {}
        self.instances: dict[str, dict] = {"i-foreign": {"InstanceId": "i-foreign", "State": {"Name": "running"},
                                                         "Tags": [{"Key": "Name", "Value": "prod"}]}}
        self.n = 0
        self.puts = 0

    def _id(self, p):
        self.n += 1
        return f"{p}-{self.n:04d}"

    # --- sts / ebs (self acts as both clients)
    def identity(self): return {"Account": "123"}

    def start_snapshot(self, VolumeSize, Timeout, Description, Tags, ParentSnapshotId=None):
        sid = self._id("snap")
        self.snaps[sid] = {"SnapshotId": sid, "State": "pending", "VolumeSize": VolumeSize, "Tags": list(Tags),
                           "parent": ParentSnapshotId, "blocks": {}, "StartTime": T0 + timedelta(seconds=self.n)}
        return {"SnapshotId": sid, "Status": "pending"}

    def put_snapshot_block(self, SnapshotId, BlockIndex, BlockData, DataLength, Checksum, ChecksumAlgorithm):
        assert len(BlockData) == DataLength == BLOCK and self.snaps[SnapshotId]["State"] == "pending"
        assert base64.b64encode(hashlib.sha256(BlockData).digest()).decode() == Checksum
        self.snaps[SnapshotId]["blocks"][BlockIndex] = bytes(BlockData)
        self.puts += 1
        return {}

    def complete_snapshot(self, SnapshotId, ChangedBlocksCount):
        s = self.snaps[SnapshotId]
        assert ChangedBlocksCount == len(s["blocks"]), (ChangedBlocksCount, len(s["blocks"]))
        s["State"] = "completed"
        return {"Status": "completed"}

    def _all_blocks(self, sid):
        s = self.snaps[sid]
        base = self._all_blocks(s["parent"]) if s["parent"] else {}
        return {**base, **s["blocks"]}

    def list_snapshot_blocks(self, SnapshotId, MaxResults, StartingBlockIndex=0, NextToken=None):
        start = int(NextToken) if NextToken else StartingBlockIndex
        idx = sorted(i for i in self._all_blocks(SnapshotId) if i >= start)
        page = idx[:MaxResults]
        r = {"BlockSize": BLOCK, "VolumeSize": self.snaps[SnapshotId]["VolumeSize"],
             "Blocks": [{"BlockIndex": i, "BlockToken": f"t{i}"} for i in page]}
        if len(idx) > MaxResults:
            r["NextToken"] = str(page[-1] + 1)
        return r

    def get_snapshot_block(self, SnapshotId, BlockIndex, BlockToken):
        data = self._all_blocks(SnapshotId)[BlockIndex]
        return {"BlockData": io.BytesIO(data), "Checksum": base64.b64encode(hashlib.sha256(data).digest()).decode(),
                "ChecksumAlgorithm": "SHA256"}

    # --- ec2
    def get_paginator(self, name):
        assert name == "describe_snapshots"
        return Pager(self.describe_snapshots)

    def describe_snapshots(self, SnapshotIds=None, Filters=None, OwnerIds=None):
        out = []
        for s in self.snaps.values():
            if SnapshotIds and s["SnapshotId"] not in SnapshotIds:
                continue
            tg = {t["Key"]: t["Value"] for t in s["Tags"]}
            ok = True
            for f in Filters or []:
                if f["Name"].startswith("tag:"):
                    ok &= tg.get(f["Name"][4:]) in f["Values"]
                elif f["Name"] == "snapshot-id":
                    ok &= s["SnapshotId"] in f["Values"]
            if ok:
                out.append({k: v for k, v in s.items() if k not in ("blocks", "parent")})
        return {"Snapshots": out}

    def create_tags(self, Resources, Tags):
        for r in Resources:
            s = self.snaps[r]
            s["Tags"] = [t for t in s["Tags"] if t["Key"] not in {x["Key"] for x in Tags}] + list(Tags)

    def delete_tags(self, Resources, Tags):
        for r in Resources:
            self.snaps[r]["Tags"] = [t for t in self.snaps[r]["Tags"] if t["Key"] not in {x["Key"] for x in Tags}]

    def delete_snapshot(self, SnapshotId):
        assert not any(SnapshotId in [b["Ebs"]["SnapshotId"] for b in i["BlockDeviceMappings"]]
                       for i in self.images.values() if i["State"] == "available"), "snapshot still used by an AMI"
        self.snaps.pop(SnapshotId)

    def describe_subnets(self, MaxResults=None):
        return {"Subnets": [{"SubnetId": "subnet-1", "CidrBlock": "10.0.0.0/24", "AvailabilityZone": "eu-west-1a",
                             "Tags": [{"Key": "Name", "Value": "app"}]}]}

    def describe_security_groups(self): return {"SecurityGroups": [{"GroupId": "sg-1", "GroupName": "default"}]}

    def register_image(self, TagSpecifications, **req):
        ami = self._id("ami")
        for b in req["BlockDeviceMappings"]:
            assert self.snaps[b["Ebs"]["SnapshotId"]]["State"] == "completed"
        self.images[ami] = {**req, "State": "available"}
        return {"ImageId": ami}

    def describe_images(self, ImageIds): return {"Images": [self.images[i] for i in ImageIds]}
    def deregister_image(self, ImageId): self.images[ImageId]["State"] = "deregistered"

    def run_instances(self, ImageId, InstanceType, MinCount, MaxCount, NetworkInterfaces, TagSpecifications, **kw):
        iid = self._id("i")
        img = self.images[ImageId]
        bdm = [{"DeviceName": b["DeviceName"], "Ebs": {"VolumeId": self._id("vol")}} for b in img["BlockDeviceMappings"]]
        self.instances[iid] = {"InstanceId": iid, "State": {"Name": "running"}, "InstanceType": InstanceType,
                               "Tags": TagSpecifications[0]["Tags"], "BlockDeviceMappings": bdm[::-1],
                               "NetworkInterfaces": NetworkInterfaces, "image": dict(img)}
        return {"Instances": [{"InstanceId": iid}]}

    def describe_instances(self, Filters):
        out = []
        for i in self.instances.values():
            tg = {t["Key"]: t["Value"] for t in i.get("Tags") or []}
            ok = True
            for f in Filters:
                if f["Name"] == "instance-id":
                    ok &= i["InstanceId"] in f["Values"]
                elif f["Name"] == "tag:Name":
                    ok &= tg.get("Name") in f["Values"]
                elif f["Name"] == "instance-state-name":
                    ok &= i["State"]["Name"] in f["Values"]
            if ok:
                out.append(i)
        return {"Reservations": [{"Instances": out}] if out else []}

    def describe_instance_status(self, InstanceIds):
        return {"InstanceStatuses": [{"SystemStatus": {"Status": "ok"}, "InstanceStatus": {"Status": "ok"}}]}

    def get_console_output(self, InstanceId, Latest): return {"Output": ""}
    def terminate_instances(self, InstanceIds):
        for i in InstanceIds:
            self.instances[i]["State"] = {"Name": "terminated"}


class Client:
    def __init__(self, fake):
        self.ec2 = self.ebs = fake
        self.identity = fake.identity


def _tgt(ctx=None, **cfg):
    ctx = ctx or RunContext(dry_run=False, logger=logging.getLogger("aws"), run_id="run-abcdef123456")
    fake = FakeAws()
    conf = {"type": "aws", "region": "eu-west-1", "network_map": {"net0": "subnet-1"}, "poll_interval": 0, **cfg}
    return T.AwsTarget("aws", conf, ctx, client=Client(fake)), fake


def _spec(**kw):
    base = dict(name="web01", source_id="vm-1", cpu=CpuSpec(sockets=1, cores=4), memory_bytes=16 << 30,
                disks=[DiskSpec(id="d0", bus=DiskBus.SCSI, size_bytes=1 << 30, order=0, is_boot=True)],
                nics=[NicSpec(id="n0", network="net0")], firmware=Firmware.UEFI, secure_boot=True,
                guest_os=GuestOS(OsFamily.LINUX))
    base.update(kw)
    return VmSpec(**base)


def test_writer_blocks_partials_and_parent():
    fake = FakeAws()
    sid = fake.start_snapshot(1, 60, "", [])["SnapshotId"]
    w = EbsSnapshotWriter(fake, sid, 4 * BLOCK, sleep=lambda s: None)
    assert w.prezeroed and not w.resumable
    w.write(0, b"A" * BLOCK)                         # whole block: put at once
    w.write(BLOCK + 10, b"B" * 100)                  # partial: held, completed with zeros at flush
    w.close()
    fake.complete_snapshot(sid, w.changed_blocks)
    assert fake.snaps[sid]["blocks"][1] == bytes(10) + b"B" * 100 + bytes(BLOCK - 110)
    # child snapshot: a partial write keeps the parent's bytes around it
    from vmigrate.providers.aws.ebs import EbsSnapshotReader
    child = fake.start_snapshot(1, 60, "", [], ParentSnapshotId=sid)["SnapshotId"]
    w2 = EbsSnapshotWriter(fake, child, 4 * BLOCK, parent_reader=EbsSnapshotReader(fake, sid, 4 * BLOCK), sleep=lambda s: None)
    assert not w2.prezeroed
    w2.write(5, b"C" * 5)
    w2.write(2 * BLOCK, b"D" * (BLOCK // 2))
    w2.write(2 * BLOCK + BLOCK // 2, b"E" * (BLOCK // 2))   # completes block 2 from two halves
    w2.close()
    fake.complete_snapshot(child, w2.changed_blocks)
    assert fake._all_blocks(child)[0] == b"A" * 5 + b"C" * 5 + b"A" * (BLOCK - 10)
    assert fake._all_blocks(child)[2] == b"D" * (BLOCK // 2) + b"E" * (BLOCK // 2)
    assert set(fake.snaps[child]["blocks"]) == {0, 2}


def test_pure_helpers():
    assert T.pick_instance_type(4, 16 << 10) == "m6i.xlarge" and T.pick_instance_type(3, 1) == "m6i.xlarge"
    r = T.image_request(_spec(), ["snap-a", "snap-b"], name="n", volume_type="gp3", arch="x86_64")
    assert r["BootMode"] == "uefi" and r["TpmSupport"] == "v2.0" and r["EnaSupport"]
    assert [b["DeviceName"] for b in r["BlockDeviceMappings"]] == ["/dev/sda1", "/dev/sdf"]
    assert T.disk_id("x") == T.disk_id("x") != T.disk_id("y")


def test_full_migration_to_aws(tmp_path):
    assert TARGETS["aws"][1] == "implemented"
    ctx = RunContext(dry_run=False, logger=logging.getLogger("aws-e2e"), run_id="run-abcdef123456")
    src = FakeSource("src", {"type": "fake", "vms": [fake_vm(disks=2, size="3MiB")]}, ctx)
    t, fake = _tgt(ctx, default_subnet="subnet-1")
    eng = EngineConfig(state_dir=tmp_path / "state", report_dir=tmp_path / "rep", chunk_size=1 << 20,
                       verify_samples=4, verify_sample_bytes=64 << 10, boot_timeout=5, max_parallel_disks=2,
                       verify_full=True)
    job = MigrationConfig(name="m", source="src", target="aws", vm="RIS-Sainath",
                          osmorph={"cloud_drivers_ready": True}, target_options={"network_map": {}})
    st = MigrationRunner(job, eng, src, t, ctx, StateStore(eng.state_dir)).run()
    assert st.phase == Phase.DONE, st.errors
    assert all(d.verify["full"]["ok"] for d in st.disks.values())
    inst = next(i for i in fake.instances.values() if i["InstanceId"] != "i-foreign")
    assert inst["InstanceType"] == "m6i.large" and len(inst["image"]["BlockDeviceMappings"]) == 2
    assert all(i["State"] == "deregistered" for i in fake.images.values())
    assert not fake.snaps                                            # migration snapshots removed after launch
    assert st.verification["boot"]["ok"] and "status checks" in st.verification["boot"]["detail"]


def test_cloud_osmorph_blocks_until_confirmed(tmp_path):
    ctx = RunContext(dry_run=False, logger=logging.getLogger("aws-gate"), run_id="run-gate00000001")
    src = FakeSource("src", {"type": "fake", "vms": [fake_vm(disks=1, size="3MiB")]}, ctx)
    t, fake = _tgt(ctx, default_subnet="subnet-1")
    eng = EngineConfig(state_dir=tmp_path / "state", report_dir=tmp_path / "rep", chunk_size=1 << 20,
                       verify_samples=2, verify_sample_bytes=64 << 10, boot_timeout=5)
    job = MigrationConfig(name="m", source="src", target="aws", vm="RIS-Sainath", target_options={"network_map": {}})
    with pytest.raises(SafetyGateError, match="cloud_drivers_ready"):
        MigrationRunner(job, eng, src, t, ctx, StateStore(eng.state_dir)).run()
    assert not fake.snaps and not [i for i in fake.instances if i != "i-foreign"]    # stopped before any upload


def test_replica_child_snapshots_and_restore_points():
    t, fake = _tgt()
    d = t.create_disk("rep-r00", 1 << 20, DiskHints(bus=DiskBus.SCSI, is_boot=True, order=0))
    with t.open_disk_writer(d) as w:
        w.write(0, b"1" * BLOCK)
    t.commit_disk(d, None)
    first = t._current("rep-r00")["SnapshotId"]
    rp = t.snapshot_disk(d, "rp-1")
    with t.open_disk_writer(d, incremental=True) as w:
        assert not w.prezeroed
        w.write(BLOCK, b"2" * BLOCK)
    t.commit_disk(d, None)
    second = t._current("rep-r00")["SnapshotId"]
    assert second != first and fake.snaps[second]["parent"] == first and first in fake.snaps   # rp kept
    with t.open_disk_reader(d) as r:
        assert r.read(0, 4) == b"1111" and r.read(BLOCK, 4) == b"2222"
    with t.open_disk_writer(d, incremental=True) as w:
        w.write(0, b"3" * BLOCK)
    t.commit_disk(d, None)
    assert second not in fake.snaps                                  # superseded, not a restore point
    clone = t.clone_disk(d, "deploy", restore_point=rp)
    vm = t.create_vm(_spec(), clone, [])
    inst = fake.instances[vm.id]
    assert inst["image"]["BlockDeviceMappings"][0]["Ebs"]["SnapshotId"] == first
    assert first in fake.snaps and t.list_attached_disks(vm) == [clone.id]
    t.delete_disk_snapshot(d, rp)
    assert first not in fake.snaps
    t.delete_disk(d)
    assert not fake.snaps


def test_resume_restarts_and_cleanup_safety():
    t, fake = _tgt()
    assert t.open_disk_writer(t.create_disk("x", 1 << 20, DiskHints(bus=DiskBus.SCSI, is_boot=True, order=0))).resumable is False
    with pytest.raises(SafetyGateError):
        t.cleanup_on_failure({"target_vm": {"id": "i-foreign", "name": "prod"}})
    assert t.find_vm("prod") == VmRef("i-foreign", "prod", "aws")
    assert t.list_networks()[0]["id"] == "subnet-1"
    p = t.preflight(_spec(nics=[NicSpec(id="n", network="lan-x")]))
    assert any("lan-x" in x for x in p)

