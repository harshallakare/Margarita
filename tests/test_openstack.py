"""Purpose: OpenStack source tests without a cloud: pure mapping + refusals, RBD names, snapshot sets
(volume-backed via Nova createImage, image-backed + forced volume snapshots), cleanup guards, the
attach transport's full lifecycle (temp volume -> attach -> device appears -> read -> detach ->
delete) against a fake /dev/disk/by-id, and rbd-diff vs compare replicas.

Cinder sizes are whole GiB; tests shrink the unit to 1 MiB (monkeypatch mapping.GIB) to stay fast.

Usage: pytest tests/test_openstack.py
"""

from __future__ import annotations

import json
import logging
import re
import uuid

import pytest

from vmigrate.config.loader import EngineConfig, MigrationConfig
from vmigrate.core.context import RunContext
from vmigrate.core.errors import DiskCountMismatch, ProviderError, SafetyGateError
from vmigrate.engine import MigrationRunner, StateStore
from vmigrate.engine.dag import DagRunner, MemoryTaskStore
from vmigrate.engine.replica import MemoryReplicaStore, ReplicaRunner, sync_dag
from vmigrate.engine.safety import check_disk_counts
from vmigrate.ir import DiskBus, OsFamily
from vmigrate.providers.fake.provider import FakeTarget, PatternDisk
from vmigrate.providers.openstack import mapping
from vmigrate.providers.openstack.source import AttachedVolumeReader, OpenstackSource

N = 13
SIZE_GIB = 2
SIZE = SIZE_GIB << 20       # with mapping.GIB patched to 1 MiB


@pytest.fixture(autouse=True)
def small_gib(monkeypatch):
    monkeypatch.setattr(mapping, "GIB", 1 << 20)


def dev(i: int) -> str:
    return "/dev/vd" + ("abcdefghijklmnopqrstuvwxyz"[i] if i < 26 else "a" + "abcdefghijklmnopqrstuvwxyz"[i - 26])


class FakeCloud:
    def __init__(self, image_backed: bool = False, n: int = N, agent: bool = True):
        self.image_backed, self.n, self.gen = image_backed, n, 0
        first = 1 if image_backed else 0
        self.vols = [{"id": f"vol-{i}", "name": f"data{i}", "size": SIZE_GIB, "bootable": i == 0 and not image_backed,
                      "attachments": [{"server_id": "srv-1", "device": dev(i)}],
                      "image_meta": {"hw_disk_bus": "scsi", "hw_scsi_model": "virtio-scsi", "os_type": "linux",
                                     "os_distro": "rhel", "hw_qemu_guest_agent": "yes" if agent else "no"}}
                     for i in range(first, n)]
        self.props = {"hw_disk_bus": "virtio", "os_type": "linux", "os_distro": "ubuntu",
                      "hw_qemu_guest_agent": "yes" if agent else "no"}
        self.images: dict[str, dict] = {}
        self.snaps: dict[str, dict] = {}
        self.deleted_images: list[str] = []
        self.deleted_snaps: list[str] = []
        self.k = 0
        self.temp_vols: dict[str, dict] = {}
        self.events: list[str] = []

    def project_id(self):
        return "proj"

    def servers(self):
        return [{"id": "srv-1", "name": "RIS-Sainath", "status": "ACTIVE"}]

    def server(self, sid):
        return {"id": "srv-1", "name": "RIS-Sainath", "flavor": {"vcpus": 4, "ram": 8192, "disk": SIZE_GIB,
                                                                  "ephemeral": 0, "swap": 0},
                "image_id": "img-base" if self.image_backed else None, "root_device_name": "/dev/vda"}

    def nova_attachments(self, sid):
        return [{"volume_id": v["id"], "device": v["attachments"][0]["device"]} for v in self.vols]

    def volumes_for(self, sid):
        return self.vols

    def image(self, iid):
        if iid == "img-base":
            return {"id": iid, "props": self.props, "virtual_size": SIZE, "bdm": []}
        return self.images[iid]

    def interfaces(self, sid):
        return [{"port_id": "p1", "mac": "fa:16:3e:00:00:01", "network": "prod-net"}]

    def create_server_image(self, sid, name):
        self.k += 1
        iid = f"img-{self.k}"
        bdm = []
        if not self.image_backed:
            for v in self.vols:
                s = f"snap-{self.k}-{v['id']}"
                self.snaps[s] = {"gen": self.gen, "name": f"snapshot for {name}", "vol": v["id"]}
                bdm.append({"device_name": v["attachments"][0]["device"], "snapshot_id": s, "boot_index": 0})
        img = {"id": iid, "name": name, "props": {}, "bdm": bdm, "disk_format": "raw", "gen": self.gen,
               "direct_url": f"rbd://fsid/images/{iid}/snap" if self.image_backed else None}
        self.images[iid] = img
        return img

    def create_volume_snapshot(self, vol, name):
        self.k += 1
        s = f"own-{self.k}-{vol}"
        self.snaps[s] = {"gen": self.gen, "name": name, "vol": vol}
        return s

    def delete_snapshot(self, sid, created_by_image=None):
        name = self.snaps[sid]["name"]
        assert name.startswith("vmigrate-") or name == f"snapshot for {created_by_image}"
        self.deleted_snaps.append(sid)
        self.snaps.pop(sid)

    def delete_image(self, iid):
        assert self.images[iid]["name"].startswith("vmigrate-")
        self.deleted_images.append(iid)
        self.images.pop(iid)

    # attach transport
    def volume_from(self, *, name, size_gib, snapshot_id=None, image_id=None):
        vid = str(uuid.uuid4())             # real Cinder ids: devices are named by their first 20 chars
        self.temp_vols[vid] = {"snapshot_id": snapshot_id, "image_id": image_id, "size": size_gib}
        self.events.append("create")
        return vid

    def attach(self, server, vid):
        self.events.append("attach")
        if self.on_attach:
            self.on_attach(vid)

    def detach(self, server, vid):
        self.events.append("detach")
        if self.on_detach:
            self.on_detach(vid)

    def delete_volume(self, vid):
        self.events.append("delete")
        self.temp_vols.pop(vid)

    on_attach = None
    on_detach = None


def content_for(cloud, sd_target: str) -> bytes:
    m = re.search(r"volume-(vol-\d+)@snapshot-(\S+?):", sd_target)
    if m:
        vol, snap = m.groups()
        i = int(vol.split("-")[1])
        return PatternDisk(f"RIS-Sainath:{i}", SIZE, generation=cloud.snaps[snap]["gen"]).read(0, SIZE)
    m = re.search(r"rbd:images/(img-\d+)@snap:", sd_target)
    return PatternDisk("RIS-Sainath:0", SIZE, generation=cloud.images[m.group(1)]["gen"]).read(0, SIZE)


class Bytes:
    def __init__(self, b):
        self.b = b

    size = property(lambda s: len(s.b))

    def read(self, off, n):
        return self.b[off:off + n]

    def allocated_ranges(self):
        return None

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class FakeShell:
    def __init__(self):
        self.diffs: dict[str, list] = {}
        self.cmds: list[list[str]] = []

    def run(self, argv, check=True, timeout=None):
        self.cmds.append(argv)
        spec = next(a for a in argv if "@snapshot-" in a)
        return json.dumps(self.diffs.get(spec.split("/volume-")[1].split("@")[0], []))


def make(tmp_path, cloud, *, transport="rbd", shell=None, attach_factory=None):
    ctx = RunContext(dry_run=False, logger=logging.getLogger("t"))
    src = OpenstackSource("os", {"type": "openstack", "auth_url": "https://k:5000/v3", "username": "u",
                                 "project_id": "p", "transport": transport, "worker_server_id": "worker-1"}, ctx,
                          cloud=cloud, shell_factory=lambda: shell or FakeShell(),
                          export_factory=lambda sh, plan, tag, snap, disk: Bytes(content_for(cloud, plan["nbd_target"])),
                          attach_factory=attach_factory)
    tgt = FakeTarget("tgt", {"staging_dir": str(tmp_path / "tgt"), "persist": True}, ctx)
    e = EngineConfig(state_dir=tmp_path / "s", report_dir=tmp_path / "r", chunk_size=1 << 20, checkpoint_every=2 << 20,
                     verify_samples=4, verify_sample_bytes=64 << 10)
    return ctx, src, tgt, e


# ---------------------------------------------------------------------- mapping

def test_mapping_volume_backed_13_disks():
    c = FakeCloud()
    srv, atts = c.server("srv-1"), c.nova_attachments("srv-1")
    spec = mapping.to_vmspec(srv, atts, c.vols, c.vols[0]["image_meta"], c.interfaces("srv-1"))
    assert len(spec.disks) == 13 and spec.boot_disk().label == "vda" and spec.ordered_disks()[12].label == "vdm"
    assert {d.controller for d in spec.disks} == {"virtio-scsi"} and spec.disks[0].bus == DiskBus.SCSI
    assert spec.guest_os.family == OsFamily.LINUX and spec.memory_bytes == 8 << 30 and spec.cpu.total == 4
    assert check_disk_counts(spec, mapping.crosscheck(srv, atts, c.vols))
    with pytest.raises(DiskCountMismatch):                       # Cinder sees one disk fewer than Nova
        check_disk_counts(spec, mapping.crosscheck(srv, atts, c.vols[:-1]))
    assert sorted(["/dev/vdaa", "/dev/vdz", "/dev/vdb"], key=mapping.device_key) == ["/dev/vdb", "/dev/vdz", "/dev/vdaa"]


def test_mapping_image_backed_and_refusals():
    c = FakeCloud(image_backed=True)
    srv, atts = c.server("srv-1"), c.nova_attachments("srv-1")
    spec = mapping.to_vmspec(srv, atts, c.vols, c.props, [])
    assert spec.boot_disk().source_handle["kind"] == "image_root" and len(spec.disks) == 13
    assert spec.boot_disk().controller == "virtio-blk"
    with pytest.raises(mapping.OpenstackMappingError, match="EPHEMERAL"):
        mapping.to_vmspec({**srv, "flavor": {**srv["flavor"], "ephemeral": 20}}, atts, c.vols, c.props, [])
    with pytest.raises(mapping.OpenstackMappingError, match="not visible in Cinder"):
        mapping.to_vmspec(srv, atts, c.vols[1:], c.props, [])
    assert mapping.rbd_volume_spec({"ceph_user": "cinder"}, "v1", "s1").startswith("rbd:volumes/volume-v1@snapshot-s1:conf=")
    assert mapping.rbd_image_spec({}, "rbd://fsid/images/abc/snap").startswith("rbd:images/abc@snap:")
    with pytest.raises(mapping.OpenstackMappingError, match="attach"):
        mapping.rbd_image_spec({}, "file:///var/lib/glance/images/abc")


# ---------------------------------------------------------------------- engine runs

def test_volume_backed_migration_over_rbd(tmp_path):
    cloud = FakeCloud()
    ctx, src, tgt, e = make(tmp_path, cloud)
    st = MigrationRunner(MigrationConfig(name="m", source="os", target="tgt", vm="RIS-Sainath"), e, src, tgt, ctx,
                         StateStore(e.state_dir)).run()
    assert st.phase.value == "done" and st.morph["status"] != "manual_required"    # virtio-scsi: no injection
    assert len(tgt.vms["RIS-Sainath"]["disks"]) == 13
    for i in range(13):
        assert tgt._final(tgt.disks[f"RIS-Sainath-disk{i:02d}"]).read_bytes() == PatternDisk(f"RIS-Sainath:{i}", SIZE).read(0, SIZE)
    assert cloud.deleted_images == ["img-1"] and len(cloud.deleted_snaps) == 13 and not cloud.snaps and not cloud.images


def test_image_backed_refuses_app_consistent_then_runs_crash_consistent(tmp_path):
    cloud = FakeCloud(image_backed=True)
    ctx, src, tgt, e = make(tmp_path, cloud)
    with pytest.raises(SafetyGateError, match="cannot be snapshotted consistently"):
        MigrationRunner(MigrationConfig(name="m", source="os", target="tgt", vm="RIS-Sainath"), e, src, tgt, ctx,
                        StateStore(e.state_dir)).run()
    assert not cloud.images and not cloud.snaps
    ctx, src, tgt, e = make(tmp_path, cloud)
    st = MigrationRunner(MigrationConfig(name="m2", source="os", target="tgt", vm="RIS-Sainath", app_consistent=False),
                         e, src, tgt, ctx, StateStore(e.state_dir)).run()
    assert st.phase.value == "done" and len(tgt.vms["RIS-Sainath"]["disks"]) == 13
    assert len(cloud.deleted_snaps) == 12 and all(s.startswith("own-") for s in cloud.deleted_snaps)
    assert not cloud.snaps and not cloud.images


def test_app_consistent_requires_guest_agent(tmp_path):
    cloud = FakeCloud(agent=False)
    ctx, src, tgt, e = make(tmp_path, cloud)
    with pytest.raises(SafetyGateError, match="hw_qemu_guest_agent"):
        MigrationRunner(MigrationConfig(name="m", source="os", target="tgt", vm="RIS-Sainath"), e, src, tgt, ctx,
                        StateStore(e.state_dir)).run()
    assert not cloud.images


def test_attach_reader_lifecycle(tmp_path):
    cloud = FakeCloud()
    byid = tmp_path / "by-id"
    byid.mkdir()
    data = PatternDisk("att", SIZE).read(0, SIZE)
    cloud.on_attach = lambda vid: (byid / f"virtio-{vid[:20]}").write_bytes(data)
    cloud.on_detach = lambda vid: (byid / f"virtio-{vid[:20]}").unlink()
    r = AttachedVolumeReader(cloud, "worker-1", name="vmigrate-r-00", size_bytes=SIZE, snapshot_id="snap-x",
                             by_id_dir=str(byid))
    assert r.size == SIZE and r.read(4096, 1000) == data[4096:5096]
    r.close()
    assert cloud.events == ["create", "attach", "detach", "delete"] and not cloud.temp_vols
    cloud.events, cloud.on_attach, cloud.on_detach = [], None, None   # device never appears -> still cleaned up
    with pytest.raises(ProviderError, match="no device appeared"):
        AttachedVolumeReader(cloud, "worker-1", name="vmigrate-r-01", size_bytes=SIZE, snapshot_id="snap-x",
                             by_id_dir=str(byid), dev_timeout=0.2)
    assert cloud.events == ["create", "attach", "detach", "delete"] and not cloud.temp_vols


def _sync(tmp_path, cloud, store, shell):
    ctx, src, tgt, e = make(tmp_path, cloud, shell=shell)
    rr = ReplicaRunner(MigrationConfig(name="rep", source="os", target="tgt", vm="RIS-Sainath"), e, src, tgt, ctx, store)
    ts = MemoryTaskStore(sync_dag(False))
    assert DagRunner(ts, rr.sync_handlers()).run().value == "completed", [t.error for t in ts.load_tasks() if t.error]
    return rr, tgt


def test_rbd_diff_replica_volume_backed(tmp_path):
    cloud, store, shell = FakeCloud(), MemoryReplicaStore(), FakeShell()
    rr, _ = _sync(tmp_path, cloud, store, shell)
    assert len(cloud.images) == 1                                 # snapshot set kept as the diff base
    cloud.gen = 1
    shell.diffs = {f"vol-{i}": [{"offset": b * (64 << 10), "length": 64 << 10, "exists": "true"}
                                for b in PatternDisk(f"RIS-Sainath:{i}", SIZE).changed_blocks(0, 1)] for i in range(13)}
    rr, tgt = _sync(tmp_path, cloud, store, shell)
    assert rr.state.syncs[-1]["modes"] == ["cbt"] and rr.state.syncs[-1]["bytes_read"] < 13 * SIZE // 3
    assert any("--from-snap" in c for c in shell.cmds)
    assert cloud.deleted_images == ["img-1"] and list(cloud.images) == ["img-2"]      # base rotated
    for i in range(13):
        assert tgt._final(tgt.disks[f"rep-r{i:02d}"]).read_bytes() == PatternDisk(f"RIS-Sainath:{i}", SIZE, generation=1).read(0, SIZE)


def test_image_backed_replica_uses_compare(tmp_path):
    cloud, store = FakeCloud(image_backed=True), MemoryReplicaStore()
    cloud.props["hw_qemu_guest_agent"] = "no"
    ctx_job = {"app_consistent": False}
    for g in (0, 1):
        cloud.gen = g
        ctx, src, tgt, e = make(tmp_path, cloud)
        rr = ReplicaRunner(MigrationConfig(name="rep", source="os", target="tgt", vm="RIS-Sainath", **ctx_job), e,
                           src, tgt, ctx, store)
        assert DagRunner(MemoryTaskStore(sync_dag(False)), rr.sync_handlers()).run().value == "completed"
    assert rr.state.syncs[-1]["modes"] == ["compare"] and not cloud.images and not cloud.snaps
