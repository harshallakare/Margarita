"""Purpose: unit tests for pure logic — IR validation/round-trip, byte-range math, copier, sampled
verification, config validation + secret interpolation, fstab hardening, OSMorph registry.

Usage: pytest tests/test_pure_logic.py
"""

from __future__ import annotations

import pytest

from vmigrate.config import build_config, parse_size
from vmigrate.core.context import RunContext
from vmigrate.core.errors import ConfigError
from vmigrate.core.fileio import RawFileReader, RawFileWriter
from vmigrate.core.ranges import ByteRange as R
from vmigrate.core.ranges import align, normalize, split, subtract, total
from vmigrate.engine.copier import copy_disk
from vmigrate.engine.safety import check_disk_counts
from vmigrate.engine.verify import compare_samples, sample_offsets
from vmigrate.ir import CpuSpec, DiskBus, DiskSpec, IRValidationError, OsFamily, VmSpec
from vmigrate.osmorph import MorphRequest, resolve
from vmigrate.osmorph.fstab import harden_fstab
from vmigrate.providers.fake.provider import PatternDisk


def spec(n=3, **kw):
    disks = [DiskSpec(id=f"d{i}", bus=DiskBus.SCSI, size_bytes=1 << 20, order=i, is_boot=i == 0, bus_index=i) for i in range(n)]
    return VmSpec(name="vm", source_id="x", cpu=CpuSpec(1, 2), memory_bytes=1 << 30, disks=disks, **kw)


# ---------------------------------------------------------------- IR
def test_ir_roundtrip_and_validate():
    s = spec(13)
    s.validate()
    s2 = VmSpec.from_dict(s.to_dict())
    assert s2 == s and s2.disks[12].address == "scsi.12"


@pytest.mark.parametrize("mutate,msg", [
    (lambda s: setattr(s.disks[1], "is_boot", True), "boot disk"),
    (lambda s: setattr(s.disks[2], "order", 7), "orders"),
    (lambda s: setattr(s.disks[2], "id", "d0"), "duplicate"),
    (lambda s: setattr(s.disks[0], "size_bytes", 0), "non-positive"),
])
def test_ir_validation_errors(mutate, msg):
    s = spec()
    mutate(s)
    with pytest.raises(IRValidationError, match=msg):
        s.validate()


def test_disk_count_gate_requires_two_methods():
    from vmigrate.core.errors import SafetyGateError
    with pytest.raises(SafetyGateError):
        check_disk_counts(spec(), {"only": 3})


# ---------------------------------------------------------------- ranges
def test_ranges():
    assert normalize([R(10, 5), R(0, 10), R(20, 1), R(14, 2)]) == [R(0, 16), R(20, 1)]
    assert align([R(5, 1)], 4, 100) == [R(4, 4)]
    assert align([R(98, 10)], 8, 100) == [R(96, 4)]
    assert split([R(0, 10)], 4) == [R(0, 4), R(4, 4), R(8, 2)]
    assert total([R(0, 10), R(5, 10)]) == 15
    assert subtract([R(0, 100)], [R(10, 10), R(50, 60)]) == [R(0, 10), R(20, 30)]


# ---------------------------------------------------------------- copier / verify
def test_copier_zero_skip_resume_and_ranges(tmp_path):
    src = PatternDisk("seed", 10 << 20)
    marks = []
    with RawFileWriter(tmp_path / "out.raw", src.size) as w:
        st = copy_disk(src, w, chunk_size=1 << 20, checkpoint_every=3 << 20, on_checkpoint=marks.append)
    assert st.bytes_read == src.size and st.bytes_zero_skipped >= 4 << 20
    assert marks[-1] == src.size and all(m % (1 << 20) == 0 for m in marks)
    with RawFileReader(tmp_path / "out.raw") as r:
        assert compare_samples(src, r, sample_offsets("x", src.size, count=20, length=4096), length=4096).ok
        assert r.read(0, src.size) == src.read(0, src.size)

    # resume from 6 MiB into a fresh file only fills the tail
    with RawFileWriter(tmp_path / "tail.raw", src.size) as w:
        st = copy_disk(src, w, chunk_size=1 << 20, start_offset=6 << 20)
    assert st.bytes_read == 4 << 20 and st.resumed_from == 6 << 20

    # changed-region copy
    with RawFileWriter(tmp_path / "inc.raw", src.size) as w:
        st = copy_disk(src, w, chunk_size=1 << 20, ranges=[R(0, 4096), R(9 << 20, 1 << 30)])
    assert st.bytes_read == 4096 + (1 << 20)


def test_copier_refuses_smaller_target(tmp_path):
    with RawFileWriter(tmp_path / "s.raw", 1 << 20) as w, pytest.raises(ValueError):
        copy_disk(PatternDisk("a", 2 << 20), w)


def test_sample_offsets_deterministic_and_cover_ends():
    a = sample_offsets("disk", 1 << 30, count=16)
    assert a == sample_offsets("disk", 1 << 30, count=16)
    assert a[0] == 0 and a[-1] == (1 << 30) - (1 << 20) and len(a) == 16


def test_sample_mismatch_detected(tmp_path):
    src = PatternDisk("a", 4 << 20)
    with RawFileWriter(tmp_path / "bad.raw", src.size) as w:
        copy_disk(src, w, chunk_size=1 << 20)
        w.write(0, b"\xff" * 512)
    with RawFileReader(tmp_path / "bad.raw") as r:
        res = compare_samples(src, r, sample_offsets("a", src.size), length=1 << 20)
    assert not res.ok and 0 in res.mismatches


# ---------------------------------------------------------------- config
BASE = {
    "engine": {"chunk_size": "64MiB", "state_dir": "/tmp/s"},
    "sources": {"ahv": {"type": "nutanix", "password": "${NTNX_PW}"}},
    "targets": {"vhi": {"type": "vhi", "password": "plaintext!"}},
    "migrations": [{"name": "m", "source": "ahv", "target": "vhi", "vm": "RIS-Sainath"}],
}


def test_config_ok_and_interpolation():
    c = build_config(BASE, env={"NTNX_PW": "s3cret"})
    assert c.engine.chunk_size == 64 << 20
    assert c.sources["ahv"]["password"] == "s3cret"
    assert any("targets.vhi.password" in w for w in c.warnings)
    assert str(c.engine.report_dir) == "/tmp/s/reports"


def test_config_missing_env_and_bad_refs():
    with pytest.raises(ConfigError, match="NTNX_PW"):
        build_config(BASE, env={})
    bad = {**BASE, "migrations": [{"name": "m", "source": "nope", "target": "vhi", "vm": "x"}]}
    with pytest.raises(ConfigError, match="unknown source"):
        build_config(bad, env={"NTNX_PW": "x"})
    with pytest.raises(ConfigError, match="unknown keys"):
        build_config({**BASE, "engine": {"chunksize": 1}}, env={"NTNX_PW": "x"})


def test_parse_size():
    assert parse_size("1GiB") == 1 << 30 and parse_size("64M") == 64 << 20 and parse_size(512) == 512
    with pytest.raises(ConfigError):
        parse_size("lots")


# ---------------------------------------------------------------- fstab / osmorph
FSTAB = """# /etc/fstab
UUID=aaa / xfs defaults 0 1
UUID=bbb /boot xfs defaults 0 2
/dev/sdb1 /data1 ext4 defaults 0 2
/dev/mapper/vg-data /data2 ext4 defaults,noatime 1 2
UUID=ccc /data3 ext4 defaults,nofail 0 0
/dev/sdc1 swap swap defaults 0 0
"""


def test_fstab_hardening():
    new, changes = harden_fstab(FSTAB, device_uuids={"/dev/sdb1": "u-b1"})
    lines = new.splitlines()
    assert lines[0] == "# /etc/fstab" and lines[1] == "UUID=aaa / xfs defaults 0 1"
    assert lines[3].split() == ["UUID=u-b1", "/data1", "ext4", "defaults,nofail", "0", "0"]
    assert lines[4].split() == ["/dev/mapper/vg-data", "/data2", "ext4", "defaults,noatime,nofail", "1", "0"]
    assert lines[5] == "UUID=ccc /data3 ext4 defaults,nofail 0 0"  # untouched
    assert lines[6] == "/dev/sdc1 swap swap defaults 0 0"
    assert len(changes) == 2
    assert harden_fstab(new)[1] == []  # idempotent


def test_osmorph_registry_and_policy():
    ctx = RunContext(dry_run=True)
    s = spec()
    s.guest_os = s.guest_os.__class__(family=OsFamily.LINUX)
    for d in s.disks:
        d.controller = "virtio-scsi"          # AHV: guest already boots from virtio-scsi
    # bus matched + virtio controller -> no required steps
    res = resolve(OsFamily.LINUX, "kvm").run(MorphRequest(s, "kvm", DiskBus.SCSI, []), ctx)
    assert res.status == "planned" and not any("REQUIRED" in x for x in res.steps)
    # same "scsi" bus but a VMware pvscsi controller -> virtio injection REQUIRED (the trap)
    for d in s.disks:
        d.controller = "pvscsi"
    res = resolve(OsFamily.LINUX, "kvm").run(MorphRequest(s, "kvm", DiskBus.SCSI, []), ctx)
    assert res.status == "manual_required" and any("inject-virtio" in x and "pvscsi" in x for x in res.steps)
    # unknown controller is treated as non-virtio (fail safe)
    for d in s.disks:
        d.controller = None
    assert resolve(OsFamily.LINUX, "kvm").run(MorphRequest(s, "kvm", DiskBus.SCSI, []), ctx).status == "manual_required"
    for d in s.disks:
        d.controller = "virtio-scsi"
    # bus changes -> virtio injection required, not automated -> manual_required
    res = resolve(OsFamily.LINUX, "kvm").run(MorphRequest(s, "kvm", DiskBus.VIRTIO, []), ctx)
    assert res.status == "manual_required"
    # Windows from a non-virtio controller with no tools here -> manual, never silently passed
    for d in s.disks:
        d.controller = "pvscsi"
    assert resolve(OsFamily.WINDOWS, "kvm").run(MorphRequest(s, "kvm", DiskBus.SCSI, []), ctx).status == "manual_required"
    # Windows already on virtio-scsi, same bus (AHV -> VHI): drivers present, nothing required
    for d in s.disks:
        d.controller = "virtio-scsi"
    assert resolve(OsFamily.WINDOWS, "kvm").run(MorphRequest(s, "kvm", DiskBus.SCSI, []), ctx).status == "planned"
    assert resolve(OsFamily.LINUX, "aws").name == "manual"
