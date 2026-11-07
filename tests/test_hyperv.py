"""Purpose: Hyper-V source tests without a Hyper-V host: pure mapping + refusals, path mapping,
PowerShell parameter hygiene, and full engine runs through HypervSource with a fake WinRM client
that writes REAL VHDX files at snapshot time (so the VHDX reader + share_map path are exercised):
migration, differencing refusal, compare-mode replica, experimental RCT replica (reference
points rotated and released), and RCT failure falling back to compare.

Usage: pytest tests/test_hyperv.py
"""

from __future__ import annotations

import base64
import json
import logging

import pytest

from vmigrate.config.loader import EngineConfig, MigrationConfig
from vmigrate.core.context import RunContext
from vmigrate.core.errors import ChangeTrackingUnavailable, DiskCountMismatch, SafetyGateError
from vmigrate.engine import MigrationRunner, StateStore
from vmigrate.engine.dag import DagRunner, MemoryTaskStore
from vmigrate.engine.replica import MemoryReplicaStore, ReplicaRunner, delete_dag, sync_dag
from vmigrate.engine.safety import check_disk_counts
from vmigrate.ir import DiskBus, Firmware, OsFamily
from vmigrate.providers.fake.provider import FakeTarget, PatternDisk
from vmigrate.providers.hyperv import client as hvclient
from vmigrate.providers.hyperv import mapping
from vmigrate.providers.hyperv.source import HypervSource

from .vdisk_builders import MIB, write_vhdx

SIZE = 3 * MIB
ROOT = "C:\\ClusterStorage\\Volume1\\VMs\\RIS"


def info(n: int = 13, gen: int = 2, **over) -> dict:
    disks = [{"id": f"disk-{i}", "controller_type": "SCSI", "controller_number": i // 8, "controller_location": i % 8,
              "path": f"{ROOT}\\disk{i}.vhdx", "disk_number": None, "shared": False, "format": "VHDX",
              "vhd_type": "Dynamic", "size": SIZE, "parent": None} for i in range(n)]
    if over:
        disks[-1].update(over)
    return {"id": "1F2E3D4C-0000-4000-8000-000000000001", "name": "RIS-Sainath", "generation": gen, "cpu": 4,
            "memory_bytes": 8 << 30, "secure_boot": True, "boot_disk_id": "disk-0",
            "os_name": "Red Hat Enterprise Linux 8.9", "version": "9.0", "disks": disks if n != 1 else disks[0],
            "nics": {"name": "Network Adapter", "switch": "vSwitch-Prod", "mac": "00155D010203", "legacy": False},
            "wmi_disk_count": n}


# ---------------------------------------------------------------------- mapping

def test_mapping_gen2_13_disks():
    spec = mapping.to_vmspec(info())
    assert len(spec.disks) == 13 and spec.boot_disk().label == "scsi0:0"
    assert spec.ordered_disks()[12].label == "scsi1:4"
    assert {d.controller for d in spec.disks} == {"hyperv-scsi"} and spec.disks[0].bus == DiskBus.SCSI
    assert spec.firmware == Firmware.UEFI and spec.secure_boot
    assert spec.guest_os.family == OsFamily.LINUX and spec.guest_os.distro == "rhel"
    assert spec.nics[0].model == "netvsc" and spec.nics[0].network == "vSwitch-Prod"   # 1-element unwrapped by PS
    assert check_disk_counts(spec, mapping.crosscheck(info()))
    with pytest.raises(DiskCountMismatch):
        check_disk_counts(spec, mapping.crosscheck({**info(), "wmi_disk_count": 10}))


def test_mapping_gen1_ide_boot_first_and_refusals():
    i = info(2, gen=1)
    i["disks"][0].update(controller_type="SCSI", controller_number=0, controller_location=0)
    i["disks"][1].update(controller_type="IDE", controller_number=0, controller_location=0)
    i["boot_disk_id"] = None
    spec = mapping.to_vmspec(i)
    assert spec.firmware == Firmware.BIOS and spec.boot_disk().label == "ide0:0" and spec.boot_disk().controller == "hyperv-ide"
    with pytest.raises(mapping.HypervMappingError, match="pass-through"):
        mapping.to_vmspec(info(path=None, disk_number=3))
    with pytest.raises(mapping.HypervMappingError, match="shared VHDX"):
        mapping.to_vmspec(info(shared=True))
    assert mapping.rct_capable({"version": "9.0"}) and not mapping.rct_capable({"version": "5.0"})


def test_local_path_mapping():
    sm = {"C:\\ClusterStorage\\Volume1": "/mnt/csv1", "C:\\ClusterStorage\\Volume1\\VMs": "/mnt/vms",
          "\\\\fs01\\vmstore": "/mnt/fs"}
    assert mapping.local_path("c:\\clusterstorage\\volume1\\VMs\\a\\d.vhdx", sm) == "/mnt/vms/a/d.vhdx"  # longest + ci
    assert mapping.local_path("C:\\ClusterStorage\\Volume1\\x.vhdx", sm) == "/mnt/csv1/x.vhdx"
    assert mapping.local_path("\\\\fs01\\vmstore\\b\\d.vhdx", sm) == "/mnt/fs/b/d.vhdx"
    with pytest.raises(mapping.HypervMappingError, match="share_map"):
        mapping.local_path("D:\\Other\\d.vhdx", sm)
    with pytest.raises(mapping.HypervMappingError):
        mapping.local_path("C:\\ClusterStorage\\Volume10\\d.vhdx", {"C:\\ClusterStorage\\Volume1": "/x"})  # no prefix trap


def test_powershell_params_are_base64_not_spliced():
    evil = "x'; Remove-VM -Force; '"
    s = hvclient.render(hvclient.VM_INFO, {"id": evil})
    assert evil not in s and "Remove-VM" not in s
    b64 = s.split("FromBase64String('")[1].split("'")[0]
    assert json.loads(base64.b64decode(b64)) == {"id": evil}


def test_rct_paging():
    calls = []

    def q(off, n):
        calls.append(off)
        return [(off + 512, 4096)], min(n, MIB)   # host processes 1 MiB per call
    r = mapping.changed_ranges(q, 3 * MIB)
    assert calls == [0, MIB, 2 * MIB] and len(r) == 3


# ---------------------------------------------------------------------- engine runs

class FakeHyperV:
    """WinRM stand-in: snapshots write VHDX files (content of the current 'generation') into the
    mounted storage, like a frozen parent disk; RCT ids encode the generation."""

    def __init__(self, mount, n: int = 13):
        self.mount, self.n, self.gen = mount, n, 0
        self.snaps: dict[str, int] = {}
        self.destroyed: list[str] = []
        self.rps: dict[str, int] = {}
        self.rp_destroyed: list[str] = []
        self.differencing = False
        self.rct_broken = False
        self.k = 0

    def ping(self):
        return {"hyperv": True, "host": "HV01"}

    def list_vms(self):
        return [{"id": info()["id"], "name": "RIS-Sainath"}]

    def vm_info(self, vm_id):
        return info(self.n)

    def _write(self, i):
        data = PatternDisk(f"RIS-Sainath:{i}", SIZE, generation=self.gen).read(0, SIZE)
        blocks = {b: data[b * MIB:(b + 1) * MIB] for b in range(3) if any(data[b * MIB:(b + 1) * MIB])}
        write_vhdx(self.mount / f"disk{i}.vhdx", SIZE, blocks)

    def create_snapshot(self, vm_id, consistency):
        self.k += 1
        sid = f"Microsoft:SNAP-{self.k}"
        self.snaps[sid] = self.gen
        for i in range(self.n):
            self._write(i)
        return {"id": sid, "disks": [{"path": f"{ROOT}\\disk{i}.vhdx", "format": "VHDX",
                                      "vhd_type": "Differencing" if self.differencing else "Dynamic",
                                      "parent": None} for i in range(self.n)]}

    def destroy_snapshot(self, vm_id, sid):
        self.destroyed.append(sid)
        self.snaps.pop(sid, None)

    def convert_to_reference_point(self, sid):
        g = self.snaps.pop(sid)
        rid = f"RP-{sid}"
        self.rps[rid] = g
        return {"id": rid, "rct_ids": [f"rct:{g}:{i}" for i in range(self.n)],
                "paths": [f"{ROOT}\\disk{i}.vhdx" for i in range(self.n)]}

    def destroy_reference_point(self, rid):
        self.rp_destroyed.append(rid)
        self.rps.pop(rid, None)

    def changed(self, path, limit, off, n):
        if self.rct_broken:
            raise ChangeTrackingUnavailable("RCT id unknown (VM moved?)")
        _, g, i = limit.split(":")
        cur = max(self.snaps.values())
        blocks = PatternDisk(f"RIS-Sainath:{i}", SIZE).changed_blocks(int(g), cur)
        return [(b * (64 << 10), 64 << 10) for b in blocks if off <= b * (64 << 10) < off + n], n

    def close(self):
        pass


def _setup(tmp_path, fh, rct=False):
    ctx = RunContext(dry_run=False, logger=logging.getLogger("t"))
    src = HypervSource("hv", {"type": "hyperv", "host": "hv01", "username": "u", "password": "p", "rct": rct,
                              "share_map": {ROOT: str(fh.mount)}}, ctx, client=fh)
    tgt = FakeTarget("tgt", {"staging_dir": str(tmp_path / "tgt"), "persist": True}, ctx)
    e = EngineConfig(state_dir=tmp_path / "s", report_dir=tmp_path / "r", chunk_size=1 << 20, checkpoint_every=2 << 20,
                     verify_samples=4, verify_sample_bytes=64 << 10)
    return ctx, src, tgt, e


@pytest.fixture
def fh(tmp_path):
    m = tmp_path / "csv1"
    m.mkdir()
    return FakeHyperV(m)


def _expect(i, gen):
    return PatternDisk(f"RIS-Sainath:{i}", SIZE, generation=gen).read(0, SIZE)


def test_migration_reads_vhdx_through_share_map(tmp_path, fh):
    ctx, src, tgt, e = _setup(tmp_path, fh)
    job = MigrationConfig(name="m", source="hv", target="tgt", vm="RIS-Sainath", osmorph={"allow_manual": True})
    st = MigrationRunner(job, e, src, tgt, ctx, StateStore(e.state_dir)).run()
    assert st.phase.value == "done" and len(tgt.vms["RIS-Sainath"]["disks"]) == 13
    assert st.morph["status"] == "manual_required"          # hyperv-scsi -> virtio injection required
    for i in range(13):
        assert tgt._final(tgt.disks[f"RIS-Sainath-disk{i:02d}"]).read_bytes() == _expect(i, 0)
    assert fh.destroyed == ["Microsoft:SNAP-1"] and not fh.snaps


def test_differencing_parent_is_refused(tmp_path, fh):
    fh.differencing = True
    ctx, src, tgt, e = _setup(tmp_path, fh)
    with pytest.raises(SafetyGateError, match="differencing"):
        MigrationRunner(MigrationConfig(name="m", source="hv", target="tgt", vm="RIS-Sainath"), e, src, tgt, ctx,
                        StateStore(e.state_dir)).run()


def _sync(tmp_path, fh, store, rct):
    ctx, src, tgt, e = _setup(tmp_path, fh, rct=rct)
    rr = ReplicaRunner(MigrationConfig(name="rep", source="hv", target="tgt", vm="RIS-Sainath"), e, src, tgt, ctx, store)
    ts = MemoryTaskStore(sync_dag(False))
    assert DagRunner(ts, rr.sync_handlers()).run().value == "completed", [t.error for t in ts.load_tasks() if t.error]
    return rr, tgt


def test_replica_compare_mode_by_default(tmp_path, fh):
    store = MemoryReplicaStore()
    _sync(tmp_path, fh, store, rct=False)
    fh.gen = 1
    rr, tgt = _sync(tmp_path, fh, store, rct=False)
    assert rr.state.syncs[-1]["modes"] == ["compare"] and not fh.rps and not fh.snaps
    assert all(tgt._final(tgt.disks[f"rep-r{i:02d}"]).read_bytes() == _expect(i, 1) for i in range(13))


def test_replica_rct_reference_points_and_fallback(tmp_path, fh):
    store = MemoryReplicaStore()
    rr, _ = _sync(tmp_path, fh, store, rct=True)
    first_rp = rr.state.base_checkpoint["data"]["reference_point"]
    assert first_rp in fh.rps and not fh.snaps                          # snapshot became a reference point
    fh.gen = 1
    rr, tgt = _sync(tmp_path, fh, store, rct=True)
    assert rr.state.syncs[-1]["modes"] == ["cbt"] and rr.state.syncs[-1]["bytes_read"] < 13 * SIZE // 3
    assert fh.rp_destroyed == [first_rp] and len(fh.rps) == 1           # previous reference point rotated out
    assert all(tgt._final(tgt.disks[f"rep-r{i:02d}"]).read_bytes() == _expect(i, 1) for i in range(13))
    fh.gen, fh.rct_broken = 2, True
    rr, tgt = _sync(tmp_path, fh, store, rct=True)
    assert rr.state.syncs[-1]["modes"] == ["compare"]                   # RCT error -> correct, slower
    assert all(tgt._final(tgt.disks[f"rep-r{i:02d}"]).read_bytes() == _expect(i, 2) for i in range(13))
    ctx, src, tgt, e = _setup(tmp_path, fh, rct=True)                   # deleting the replica releases the RP
    rr = ReplicaRunner(MigrationConfig(name="rep", source="hv", target="tgt", vm="RIS-Sainath"), e, src, tgt, ctx, store)
    assert DagRunner(MemoryTaskStore(delete_dag(False)), rr.delete_handlers()).run().value == "completed"
    assert not fh.rps
