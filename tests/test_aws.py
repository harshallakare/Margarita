"""Purpose: AWS source tests without AWS: EC2 -> IR mapping + refusals, the EBS direct-API reader
(sliding token window, parallel fetch, SHA-256 verification, throttling retries, never-written
blocks skipped), and engine runs (migration, ListChangedBlocks replica) through AwsSource with a
fake EC2 + EBS. Volume sizes are whole GiB; tests shrink the unit (monkeypatch mapping.GIB).

Usage: pytest tests/test_aws.py
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging

import pytest

from vmigrate.config.loader import EngineConfig, MigrationConfig
from vmigrate.core.context import RunContext
from vmigrate.core.errors import DiskCountMismatch, SafetyGateError, TransientProviderError
from vmigrate.engine import MigrationRunner, StateStore
from vmigrate.engine.dag import DagRunner, MemoryTaskStore
from vmigrate.engine.replica import MemoryReplicaStore, ReplicaRunner, sync_dag
from vmigrate.engine.safety import check_disk_counts
from vmigrate.ir import DiskBus, Firmware, OsFamily
from vmigrate.providers.aws import mapping
from vmigrate.providers.aws.ebs import EbsSnapshotReader
from vmigrate.providers.aws.source import AwsSource
from vmigrate.providers.fake.provider import FakeTarget, PatternDisk

UNIT = 6 << 20              # one "GiB" in tests: 6 MiB, so each disk has a never-written 4-6 MiB stripe
BLOCK = 512 << 10
N = 13


@pytest.fixture(autouse=True)
def small_gib(monkeypatch):
    monkeypatch.setattr(mapping, "GIB", UNIT)


def dev(i: int) -> str:
    return "/dev/xvda" if i == 0 else "/dev/sd" + "bcdefghijklmnopqrstuvwxyz"[i - 1]


def instance(n: int = N, **over) -> dict:
    i = {"InstanceId": "i-0abc", "InstanceType": "m6i.xlarge", "RootDeviceName": "/dev/xvda",
         "Tags": [{"Key": "Name", "Value": "RIS-Sainath"}], "PlatformDetails": "Red Hat Enterprise Linux",
         "CpuOptions": {"CoreCount": 2, "ThreadsPerCore": 2}, "BootMode": "uefi",
         "BlockDeviceMappings": [{"DeviceName": dev(k), "Ebs": {"VolumeId": f"vol-{k:02d}"}} for k in range(n)],
         "NetworkInterfaces": [{"NetworkInterfaceId": "eni-1", "VpcId": "vpc-1", "SubnetId": "subnet-1",
                                "MacAddress": "02:00:00:00:00:01", "PrivateIpAddress": "10.0.0.5",
                                "Attachment": {"DeviceIndex": 0}}],
         "Placement": {"AvailabilityZone": "eu-west-1a"}}
    i.update(over)
    return i


def volumes(n: int = N) -> list[dict]:
    return [{"VolumeId": f"vol-{k:02d}", "Size": 1, "VolumeType": "gp3", "Encrypted": k == 3,
             "Attachments": [{"InstanceId": "i-0abc", "State": "attached", "Device": dev(k)}]} for k in range(n)]


ITYPE = {"InstanceType": "m6i.xlarge", "Hypervisor": "nitro", "VCpuInfo": {"DefaultVCpus": 4},
         "MemoryInfo": {"SizeInMiB": 16384}, "InstanceStorageSupported": False}


# ---------------------------------------------------------------------- mapping

def test_mapping_nitro_13_volumes():
    spec = mapping.to_vmspec(instance(), volumes(), ITYPE)
    assert len(spec.disks) == 13 and spec.boot_disk().label == "xvda" and spec.ordered_disks()[12].label == "sdm"
    assert {d.controller for d in spec.disks} == {"nvme"} and spec.disks[0].bus == DiskBus.NVME
    assert spec.firmware == Firmware.UEFI and spec.cpu.total == 4 and spec.memory_bytes == 16 << 30
    assert spec.guest_os.family == OsFamily.LINUX and spec.guest_os.distro == "rhel"
    assert spec.nics[0].model == "ena" and spec.nics[0].network == "vpc-1/subnet-1"
    assert check_disk_counts(spec, mapping.crosscheck(instance(), volumes()))
    with pytest.raises(DiskCountMismatch):
        check_disk_counts(spec, mapping.crosscheck(instance(), volumes()[:10]))


def test_mapping_refusals_and_variants():
    store = {**ITYPE, "InstanceStorageSupported": True, "InstanceStorageInfo": {"TotalSizeInGB": 237}}
    with pytest.raises(mapping.AwsMappingError, match="instance store"):
        mapping.to_vmspec(instance(), volumes(), store)
    assert mapping.to_vmspec(instance(), volumes(), store, ignore_instance_store=True)
    xen = mapping.to_vmspec(instance(Platform="windows", BootMode="legacy-bios"), volumes(), {**ITYPE, "Hypervisor": "xen"})
    assert xen.disks[0].controller == "xen-blkfront" and xen.guest_os.family == OsFamily.WINDOWS
    assert xen.firmware == Firmware.BIOS and xen.nics[0].model == "xen-netfront"
    with pytest.raises(mapping.AwsMappingError, match="not returned"):
        mapping.to_vmspec(instance(), volumes()[1:], ITYPE)


# ---------------------------------------------------------------------- fake EBS / EC2

class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeEbs:
    def __init__(self):
        self.snaps: dict[str, tuple[int, int]] = {}      # snapshot id -> (disk index, generation)
        self.throttle_next = 0
        self.corrupt_next = 0
        self.gets = 0

    def _content(self, sid: str) -> bytes:
        k, g = self.snaps[sid]
        return PatternDisk(f"RIS-Sainath:{k}", UNIT, generation=g).read(0, UNIT)

    def _blocks(self, sid: str) -> list[int]:
        d = self._content(sid)
        return [b for b in range(UNIT // BLOCK) if any(d[b * BLOCK:(b + 1) * BLOCK])]

    def list_snapshot_blocks(self, SnapshotId, MaxResults, StartingBlockIndex=0, NextToken=None):
        start = int(NextToken) if NextToken else StartingBlockIndex
        blocks = [b for b in self._blocks(SnapshotId) if b >= start]
        page = blocks[:MaxResults]
        r = {"BlockSize": BLOCK, "VolumeSize": 1, "Blocks": [{"BlockIndex": b, "BlockToken": f"t{b}"} for b in page]}
        if len(blocks) > MaxResults:
            r["NextToken"] = str(page[-1] + 1)
        return r

    def get_snapshot_block(self, SnapshotId, BlockIndex, BlockToken):
        self.gets += 1
        if self.throttle_next:
            self.throttle_next -= 1
            raise ClientError("ThrottlingException")
        assert BlockToken == f"t{BlockIndex}"
        data = self._content(SnapshotId)[BlockIndex * BLOCK:(BlockIndex + 1) * BLOCK]
        ck = base64.b64encode(hashlib.sha256(data).digest()).decode()
        if self.corrupt_next:
            self.corrupt_next -= 1
            data = b"\xff" + data[1:]
        return {"BlockData": io.BytesIO(data), "Checksum": ck, "ChecksumAlgorithm": "SHA256", "DataLength": len(data)}

    def list_changed_blocks(self, FirstSnapshotId, SecondSnapshotId, MaxResults, NextToken=None):
        a, b = self._content(FirstSnapshotId), self._content(SecondSnapshotId)
        ch = [i for i in range(UNIT // BLOCK) if a[i * BLOCK:(i + 1) * BLOCK] != b[i * BLOCK:(i + 1) * BLOCK]]
        return {"BlockSize": BLOCK, "ChangedBlocks": [{"BlockIndex": i} for i in ch]}


class FakeAws:
    def __init__(self):
        self.ebs = FakeEbs()
        self.gen = 0
        self.k = 0
        self.deleted: list[str] = []

    def identity(self):
        return {"Account": "123"}

    def instances(self):
        return [instance()]

    def instance(self, iid):
        return instance()

    def volumes_for(self, iid):
        return volumes()

    def instance_type(self, t):
        return ITYPE

    def create_snapshots(self, iid, run_tag):
        self.k += 1
        out = {}
        for v in volumes():
            sid = f"snap-{self.k}-{v['VolumeId']}"
            self.ebs.snaps[sid] = (int(v["VolumeId"][4:]), self.gen)
            out[v["VolumeId"]] = sid
        return out

    def delete_snapshot(self, sid):
        self.deleted.append(sid)
        self.ebs.snaps.pop(sid, None)


# ---------------------------------------------------------------------- reader

def test_ebs_reader_window_parallel_and_allocation():
    ebs = FakeEbs()
    ebs.snaps["s"] = (3, 0)
    want = PatternDisk("RIS-Sainath:3", UNIT).read(0, UNIT)
    r = EbsSnapshotReader(ebs, "s", UNIT, window=2, workers=4, sleep=lambda s: None)  # tiny window: many re-lists
    assert r.read(0, UNIT) == want
    assert r.read(BLOCK - 10, 2 * BLOCK) == want[BLOCK - 10:3 * BLOCK - 10]
    alloc = [(x.offset, x.length) for x in r.allocated_ranges()]
    assert alloc == [(0, 4 << 20)]                    # the 4-6 MiB stripe was never written: skipped
    r.close()


def test_ebs_reader_retries_throttling_and_checksum():
    ebs = FakeEbs()
    ebs.snaps["s"] = (1, 0)
    sleeps = []
    r = EbsSnapshotReader(ebs, "s", UNIT, workers=1, sleep=sleeps.append)
    ebs.throttle_next, ebs.corrupt_next = 3, 2
    assert r.read(0, BLOCK) == PatternDisk("RIS-Sainath:1", UNIT).read(0, BLOCK)
    assert len(sleeps) == 3                           # throttles backed off; 2 corrupt blocks re-fetched
    ebs.corrupt_next = 3
    with pytest.raises(TransientProviderError, match="checksum"):
        r.read(BLOCK, BLOCK)


# ---------------------------------------------------------------------- engine runs

def make(tmp_path, fake, **cfg):
    ctx = RunContext(dry_run=False, logger=logging.getLogger("t"))
    src = AwsSource("aws", {"type": "aws", "region": "eu-west-1", **cfg}, ctx, client=fake)
    tgt = FakeTarget("tgt", {"staging_dir": str(tmp_path / "tgt"), "persist": True}, ctx)
    e = EngineConfig(state_dir=tmp_path / "s", report_dir=tmp_path / "r", chunk_size=1 << 20, checkpoint_every=2 << 20,
                     verify_samples=4, verify_sample_bytes=64 << 10)
    return ctx, src, tgt, e


def test_migration_reads_ebs_and_cleans_up(tmp_path):
    fake = FakeAws()
    ctx, src, tgt, e = make(tmp_path, fake)
    job = MigrationConfig(name="m", source="aws", target="tgt", vm="RIS-Sainath", app_consistent=False,
                          osmorph={"allow_manual": True})
    st = MigrationRunner(job, e, src, tgt, ctx, StateStore(e.state_dir)).run()
    assert st.phase.value == "done" and st.morph["status"] == "manual_required"   # nvme -> virtio required
    for k in range(13):
        assert tgt._final(tgt.disks[f"RIS-Sainath-disk{k:02d}"]).read_bytes() == PatternDisk(f"RIS-Sainath:{k}", UNIT).read(0, UNIT)
    assert len(fake.deleted) == 13 and not fake.ebs.snaps
    assert all(d["copy_stats"]["bytes_read"] < UNIT for d in (x.__dict__ for x in st.disks.values()))  # stripe skipped


def test_app_consistent_is_refused(tmp_path):
    fake = FakeAws()
    ctx, src, tgt, e = make(tmp_path, fake)
    with pytest.raises(SafetyGateError, match="crash-consistent"):
        MigrationRunner(MigrationConfig(name="m", source="aws", target="tgt", vm="RIS-Sainath"), e, src, tgt, ctx,
                        StateStore(e.state_dir)).run()
    assert not fake.ebs.snaps


def test_list_changed_blocks_replica(tmp_path):
    fake, store = FakeAws(), MemoryReplicaStore()

    def sync():
        ctx, src, tgt, e = make(tmp_path, fake)
        rr = ReplicaRunner(MigrationConfig(name="rep", source="aws", target="tgt", vm="RIS-Sainath",
                                           app_consistent=False), e, src, tgt, ctx, store)
        ts = MemoryTaskStore(sync_dag(False))
        assert DagRunner(ts, rr.sync_handlers()).run().value == "completed", [t.error for t in ts.load_tasks() if t.error]
        return rr, tgt

    rr, _ = sync()
    assert len(fake.ebs.snaps) == 13                           # first set kept as the diff base
    fake.gen = 1
    rr, tgt = sync()
    last = rr.state.syncs[-1]
    # EBS tracks 512 KiB blocks; the fake rewrites ~1/8 of 64 KiB pieces, so ~2/3 of EBS blocks change.
    # What matters: exactly the ListChangedBlocks bytes are read — never the whole disk.
    assert last["modes"] == ["cbt"] and last["bytes_read"] == last["changed_bytes"] < 13 * UNIT
    assert len(fake.deleted) == 13 and len(fake.ebs.snaps) == 13  # base rotated
    for k in range(13):
        assert tgt._final(tgt.disks[f"rep-r{k:02d}"]).read_bytes() == PatternDisk(f"RIS-Sainath:{k}", UNIT, generation=1).read(0, UNIT)
