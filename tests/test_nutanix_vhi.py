"""Purpose: tests for the reference vertical — Nutanix AHV source mapping/snapshot/read, VHI target
volume/backing/VM logic, and the full Nutanix -> VHI pipeline through the real engine with
faked Prism, OpenStack and qemu-img.

Usage: pytest tests/test_nutanix_vhi.py
"""

from __future__ import annotations

import logging

import pytest

from vmigrate.config.loader import EngineConfig, MigrationConfig
from vmigrate.core.context import RunContext
from vmigrate.core.errors import DiskCountMismatch, DryRunViolation, LiveDiskReadRefused, SafetyGateError
from vmigrate.core.interfaces import DiskHints, VmRef
from vmigrate.engine import MigrationRunner, Phase, StateStore
from vmigrate.ir import DiskBus, Firmware
from vmigrate.providers.fake.provider import PatternDisk
from vmigrate.providers.nutanix import source as nsrc
from vmigrate.providers.nutanix.mapping import NutanixMappingError, to_vmspec
from vmigrate.providers.nutanix.source import NutanixSource
from vmigrate.providers.vhi.auth import auth_kwargs, scrub_env
from vmigrate.providers.vhi.qemu import convert_argv
from vmigrate.providers.vhi.target import (
    VhiTarget,
    _dev_key,
    backing_path,
    boot_volume_metadata,
    pick_flavor,
    round_up_gib,
)

from .fakes_platform import VM_UUID, FakeOpenStack, FakePrism, FakeQemu, v2_vm, v3_vm

DISKS13 = [(f"vd-{i:02d}", i, (i + 1) << 30) for i in range(13)]


# ================================================================ Nutanix mapping (pure)
def test_mapping_13_disks_no_cap_cdrom_excluded():
    s = to_vmspec(v3_vm("RIS-Sainath", DISKS13), v2_vm("RIS-Sainath", VM_UUID, DISKS13))
    s.validate()
    assert len(s.disks) == 13
    assert [d.bus_index for d in s.ordered_disks()] == list(range(13))
    assert {d.address for d in s.disks} >= {"scsi.10", "scsi.11", "scsi.12"}
    assert s.boot_disk().bus_index == 0
    assert s.cpu.total == 8 and s.memory_bytes == 16 << 30
    assert s.nics[0].mac == "50:6b:8d:00:00:01" and s.nics[0].network == "VLAN-120"
    assert s.guest_os.family.value == "linux"
    assert s.disks[12].source_handle["ndfs_filepath"] == "/ctr-prod/.acropolis/vmdisk/vd-12"


def test_mapping_fails_loud_when_v2_misses_a_disk():
    with pytest.raises(NutanixMappingError, match=r"only in v3 \['scsi.12'\]"):
        to_vmspec(v3_vm("x", DISKS13), v2_vm("x", VM_UUID, DISKS13[:12]))


def test_mapping_size_and_uuid_mismatch():
    bad = list(DISKS13)
    bad[5] = ("vd-05", 5, 1)
    with pytest.raises(NutanixMappingError, match="size mismatch"):
        to_vmspec(v3_vm("x", DISKS13), v2_vm("x", VM_UUID, bad))
    bad[5] = ("other", 5, DISKS13[5][2])
    with pytest.raises(NutanixMappingError, match="uuid mismatch"):
        to_vmspec(v3_vm("x", DISKS13), v2_vm("x", VM_UUID, bad))


def test_mapping_v3_uuid_differs_from_vmdisk_uuid_like_real_prism():
    # Real Prism (seen on ovin-jump): v3 disk uuid is the device id, v2 vmdisk_uuid is the storage vdisk.
    s = to_vmspec(v3_vm("x", DISKS13), v2_vm("x", VM_UUID, DISKS13))
    d = s.disks[0]
    assert d.id == "vd-00" and d.source_handle["v3_uuid"] == "dev-vd-00" != d.source_handle["vmdisk_uuid"]
    assert d.source_handle["device_uuid"] == "dev-vd-00"
    v2 = v2_vm("x", VM_UUID, DISKS13)
    for e in v2["vm_disk_info"]:
        e["disk_address"].pop("device_uuid", None)        # older AOS without device_uuid: no false mismatch
    assert len(to_vmspec(v3_vm("x", DISKS13), v2).disks) == 13


def test_mapping_volume_group_disk_is_not_silently_dropped():
    v3 = v3_vm("x", DISKS13)
    v3["status"]["resources"]["disk_list"].append({"volume_group_reference": {"uuid": "vg"},
        "device_properties": {"device_type": "DISK", "disk_address": {"adapter_type": "SCSI", "device_index": 13}}})
    with pytest.raises(NutanixMappingError, match="Volume Group"):
        to_vmspec(v3, v2_vm("x", VM_UUID, DISKS13))


def test_mapping_uefi_and_non_first_boot_disk():
    s = to_vmspec(v3_vm("x", DISKS13, boot_index=3, boot_type="SECURE_BOOT"), v2_vm("x", VM_UUID, DISKS13))
    assert s.firmware == Firmware.UEFI and s.secure_boot and s.boot_disk().bus_index == 3


# ================================================================ Nutanix source (fake Prism)
def _src(tmp_path, prism, dry=False, **cfg):
    ctx = RunContext(dry_run=dry, logger=logging.getLogger("t"))
    conf = {"type": "nutanix", "protection_domain": "pd1", "nfs_root": str(prism.nfs_root), "poll_interval": 0, **cfg}
    return NutanixSource("ahv", conf, ctx, client=prism)


def test_list_vms_paginates(tmp_path, monkeypatch):
    monkeypatch.setattr(nsrc, "PAGE", 2)
    p = FakePrism.with_vm(tmp_path, extra_vms=5)
    assert len(_src(tmp_path, p).list_vms()) == 6


def test_crosscheck_detects_capped_enumeration(tmp_path):
    p = FakePrism.with_vm(tmp_path, disks=13)
    s = _src(tmp_path, p)
    vm = s.find_vm("RIS-Sainath")
    assert s.disk_count_crosscheck(vm) == {"prism_v3.disk_list": 13, "prism_v2.vm_disk_info": 13}
    p.v2_disks_override = p.disks[:10]  # simulate an API/path that stops at 10
    assert s.disk_count_crosscheck(vm)["prism_v2.vm_disk_info"] == 10


def test_snapshot_materializes_clone_and_reads_frozen_copy(tmp_path):
    p = FakePrism.with_vm(tmp_path, disks=13)
    s = _src(tmp_path, p)
    vm = s.find_vm("RIS-Sainath")
    spec = s.inspect_vm(vm)
    snap = s.create_snapshot(vm, app_consistent=True)
    oob = [b for (path, b) in p.posts if path.endswith("oob_schedules")][0]
    assert oob["appConsistent"] is True
    assert snap.id == "4242" and s.snapshot_disk_count(snap) == 13
    assert snap.data["clone_name"].startswith("vmigrate-")
    d12 = next(d for d in spec.disks if d.bus_index == 12)
    with s.open_disk(snap, d12) as r:
        assert r.size == d12.size_bytes
        assert r.read(0, 4096) == PatternDisk("ntnx:12", d12.size_bytes).read(0, 4096)
    s.cleanup(snap)
    assert "/vms/clone-vm-uuid" in p.deleted and "/protection_domains/pd1/dr_snapshots/4242" in p.deleted


def test_open_disk_refuses_live_or_unknown(tmp_path):
    p = FakePrism.with_vm(tmp_path, disks=2)
    s = _src(tmp_path, p)
    vm = s.find_vm("RIS-Sainath")
    spec = s.inspect_vm(vm)
    snap = s.create_snapshot(vm, app_consistent=True)
    from dataclasses import replace
    live = replace(snap, data={**snap.data, "disk_paths": {spec.disks[0].id: spec.disks[0].source_handle["ndfs_filepath"]}})
    with pytest.raises(LiveDiskReadRefused):
        s.open_disk(live, spec.disks[0])
    with pytest.raises(LiveDiskReadRefused):
        s.open_disk(replace(snap, data={**snap.data, "disk_paths": {}}), spec.disks[1])


def test_snapshot_requires_pd_membership_and_execute(tmp_path):
    p = FakePrism.with_vm(tmp_path, disks=2)
    p.pd_vms = []
    vm = VmRef(VM_UUID, "RIS-Sainath")
    with pytest.raises(DryRunViolation):
        _src(tmp_path, p, dry=True).create_snapshot(vm, True)
    with pytest.raises(Exception, match="not in protection domain"):
        _src(tmp_path, p).create_snapshot(vm, True)


def test_cleanup_refuses_foreign_vm(tmp_path):
    p = FakePrism.with_vm(tmp_path, disks=2)
    s = _src(tmp_path, p)
    snap = s.create_snapshot(s.find_vm("RIS-Sainath"), True)
    from dataclasses import replace
    with pytest.raises(SafetyGateError):
        s.cleanup(replace(snap, data={**snap.data, "clone_uuid": VM_UUID}))
    assert not p.deleted


# ================================================================ VHI pure helpers
def test_vhi_helpers():
    assert round_up_gib(1) == 1 and round_up_gib(1 << 30) == 1 and round_up_gib((1 << 30) + 1) == 2
    assert str(backing_path("/mnt/vstorage", "abc")) == "/mnt/vstorage/vols/datastores/cinder/volume-abc/volume-abc"
    argv = convert_argv("/s/x.raw", "/b/volume-x")
    assert argv[:6] == ["qemu-img", "convert", "-p", "-W", "-m", "16"]
    assert "compat=1.1,cluster_size=1048576" in argv and argv[-2:] == ["/s/x.raw", "/b/volume-x"]
    assert sorted(["/dev/sdaa", "/dev/sdb", "/dev/sda", "/dev/sdz"], key=_dev_key) == ["/dev/sda", "/dev/sdb", "/dev/sdz", "/dev/sdaa"]
    assert pick_flavor([{"id": "a", "vcpus": 4, "ram": 8192}, {"id": "b", "vcpus": 8, "ram": 16384}], 8, 16384)["id"] == "b"
    assert pick_flavor([{"id": "a", "vcpus": 4, "ram": 8192}], 8, 1) is None


def test_boot_metadata():
    s = to_vmspec(v3_vm("x", DISKS13, boot_type="UEFI"), v2_vm("x", VM_UUID, DISKS13))
    md = boot_volume_metadata(s, DiskBus.SCSI)
    assert md == {"hw_disk_bus": "scsi", "hw_scsi_model": "virtio-scsi", "hw_firmware_type": "uefi", "hw_machine_type": "q35"}


def test_auth_scoping():
    with pytest.raises(Exception, match="project_id, or project_name"):
        auth_kwargs({"auth_url": "u", "username": "a"})
    # Coriolis-style: project name + project domain
    kw = auth_kwargs({"auth_url": "u", "username": "a", "password": "p", "project_name": "Medimaze",
                      "project_domain_name": "Medimaze", "user_domain_name": "Medimaze"})
    assert kw["project_name"] == "Medimaze" and kw["project_domain_name"] == "Medimaze" and "project_id" not in kw
    kw = auth_kwargs({"auth_url": "u", "username": "a", "project_name": "P", "user_domain_name": "corp"})
    assert kw["project_domain_name"] == "corp"                 # defaults to the user's domain
    with pytest.raises(Exception, match="project_domain_name"):
        auth_kwargs({"auth_url": "u", "username": "a", "project_name": "P", "user_domain_id": "d1"})
    kw = auth_kwargs({"auth_url": "u", "username": "a", "project_id": "pid", "project_name": "ignored"})
    assert kw["project_id"] == "pid" and "project_name" not in kw   # the ID wins when both are given
    kw = auth_kwargs({"auth_url": "u", "username": "a", "password": "p", "project_id": "pid", "user_domain_name": "corp"})
    assert kw["project_id"] == "pid" and kw["load_envvars"] is False and "project_name" not in kw
    assert kw["connect_retries"] == 5 and kw["status_code_retries"] == 3    # dropped connections are retried
    env = {"OS_PROJECT_NAME": "x", "OS_PROJECT_DOMAIN_ID": "y", "OS_AUTH_URL": "keep"}
    assert sorted(scrub_env(env)) == ["OS_PROJECT_DOMAIN_ID", "OS_PROJECT_NAME"] and env == {"OS_AUTH_URL": "keep"}


# ================================================================ VHI target (fake OpenStack)
def _tgt(tmp_path, dry=False, api=None, qemu=None, **cfg):
    ctx = RunContext(dry_run=dry, logger=logging.getLogger("t"))
    api = api or FakeOpenStack(tmp_path / "vstorage")
    (tmp_path / "vstorage/vols/datastores/cinder").mkdir(parents=True, exist_ok=True)
    conf = {"type": "vhi", "volume_type": "vmds", "project_id": "proj-1", "vstorage_mount": str(tmp_path / "vstorage"),
            "staging_dir": str(tmp_path / "staging"), "chown_backing": False, "poll_interval": 0,
            "network_map": {"VLAN-120": "uuid-net120"}, **cfg}
    return VhiTarget("vhi", conf, ctx, api=api, qemu=qemu or FakeQemu()), api


def test_vhi_project_by_name_and_domain_validates(tmp_path):
    t, _ = _tgt(tmp_path, project_id="", project_name="Medimaze", project_domain_name="Medimaze")
    assert t.check_connection() == []                 # no project_id to compare: Keystone scoped by name
    t2, _ = _tgt(tmp_path / "b", project_id="other")
    assert "expected other" in t2.check_connection()[0]


def test_vhi_volume_type_is_optional_cinder_default_used(tmp_path):
    t, api = _tgt(tmp_path, volume_type="")
    assert t.check_connection() == [] and not any("volume type" in x for x in t.preflight())
    ref = t.create_disk("vm-disk00", 1 << 20, DiskHints(bus=DiskBus.SCSI))
    assert api.get_volume(ref.id)["volume_type"] == "__default__"        # type omitted -> Cinder default
    t2, api2 = _tgt(tmp_path / "b", volume_type="vmds")
    assert api2.get_volume(t2.create_disk("d", 1 << 20, DiskHints(bus=DiskBus.SCSI)).id)["volume_type"] == "vmds"


def test_vhi_create_write_commit(tmp_path):
    t, api = _tgt(tmp_path)
    ref = t.create_disk("vm-disk00", (1 << 20) * 3, DiskHints(bus=DiskBus.SCSI))
    assert ref.size_bytes == 1 << 30  # rounded up to whole GiB
    with t.open_disk_writer(ref) as w:
        w.write(0, b"\x01" * 4096)
        staged = w.staged_path
    t.commit_disk(ref, staged)
    b = backing_path(tmp_path / "vstorage", ref.id)
    assert b.read_bytes()[:4] == b"QFI\xfb" and (b.parent / ".info").read_text() == "sidecar"
    with t.open_disk_reader(ref) as r:
        assert r.read(0, 4096) == b"\x01" * 4096


def test_vhi_commit_refuses_attached_or_wrong_size(tmp_path):
    t, api = _tgt(tmp_path)
    ref = t.create_disk("d", 1 << 20, DiskHints(bus=DiskBus.SCSI))
    with t.open_disk_writer(ref) as w:
        staged = w.staged_path
    api.volumes[ref.id]["attachments"] = [{"server_id": "s"}]
    with pytest.raises(SafetyGateError, match="detached"):
        t.commit_disk(ref, staged)
    api.volumes[ref.id]["attachments"] = []
    t2, _ = _tgt(tmp_path, api=api, qemu=FakeQemu(virtual_size_delta=512))
    with pytest.raises(SafetyGateError, match="virtual size"):
        t2.commit_disk(ref, staged)


def test_vhi_dry_run_refuses_mutation(tmp_path):
    t, api = _tgt(tmp_path, dry=True)
    with pytest.raises(DryRunViolation):
        t.create_disk("d", 1 << 20, DiskHints(bus=DiskBus.SCSI))
    assert not api.volumes


def test_vhi_boot_check_patterns(tmp_path):
    t, api = _tgt(tmp_path)
    api.servers["s"] = {"id": "s", "name": "x", "status": "ACTIVE"}
    assert t.check_boot(VmRef("s", "x"), timeout=1).ok
    api.console = "Starting dracut...\nYou are in emergency mode. After logging in, type"
    bc = t.check_boot(VmRef("s", "x"), timeout=1)
    assert not bc.ok and "emergency" in bc.detail


# ================================================================ full vertical: Nutanix -> VHI
def _vertical(tmp_path, *, dry=False, disks=13, prism=None, api=None):
    ctx = RunContext(dry_run=dry, logger=logging.getLogger("vertical"))
    prism = prism or FakePrism.with_vm(tmp_path, disks=disks)
    src = _src(tmp_path, prism, dry=dry)
    src.ctx = ctx
    tgt, api = _tgt(tmp_path, dry=dry, api=api)
    tgt.ctx = ctx
    eng = EngineConfig(state_dir=tmp_path / "state", report_dir=tmp_path / "reports", chunk_size=1 << 20,
                       verify_samples=6, verify_sample_bytes=64 << 10, boot_timeout=2)
    job = MigrationConfig(name="ris-sainath", source="ahv", target="vhi", vm="RIS-Sainath",
                          guest_os={"family": "linux"})
    return MigrationRunner(job, eng, src, tgt, ctx, StateStore(eng.state_dir)), prism, api


def test_vertical_dry_run(tmp_path):
    r, prism, api = _vertical(tmp_path, dry=True)
    st = r.run()
    assert st.phase == Phase.INSPECTED and st.crosscheck["prism_v2.vm_disk_info"] == 13
    assert not api.volumes and not prism.snapshots and not prism.posts


def test_vertical_execute_13_disks(tmp_path):
    r, prism, api = _vertical(tmp_path)
    st = r.run()
    assert st.phase == Phase.DONE, st.errors
    srv = next(iter(api.servers.values()))
    bdm = srv["kw"]["block_device_mapping"]
    assert len(bdm) == 13 and bdm[0]["boot_index"] == 0 and all(b["boot_index"] == -1 for b in bdm[1:])
    # BDM order follows source SCSI order, so /dev/sdX naming is preserved
    names = [api.volumes[b["uuid"]]["name"] for b in bdm]
    assert names == [f"RIS-Sainath-disk{i:02d}" for i in range(13)]
    boot_vol = bdm[0]["uuid"]
    md = [b for (v, b) in api.actions if v == boot_vol and "os-set_image_metadata" in b][0]
    assert md["os-set_image_metadata"]["metadata"]["hw_disk_bus"] == "scsi"
    assert all(d.verify["target_samples"]["ok"] for d in st.disks.values())
    assert "/vms/clone-vm-uuid" in prism.deleted
    assert not list((tmp_path / "staging").glob("*.raw"))  # staging freed


def test_vertical_gate_blocks_capped_source(tmp_path):
    prism = FakePrism.with_vm(tmp_path, disks=13)
    prism.v2_disks_override = prism.disks[:10]
    r, prism, api = _vertical(tmp_path, prism=prism)
    with pytest.raises((DiskCountMismatch, NutanixMappingError)):
        r.run()
    assert not api.volumes and not prism.snapshots


def test_vhi_boot_check_windows_does_not_wait_for_a_serial_prompt(tmp_path):
    t, api = _tgt(tmp_path, windows_boot_grace=0)
    sid = api.create_server(name="w", flavor_id="f", networks=[], block_device_mapping=[], metadata={})["id"]
    api.console = ""                                     # Windows: nothing on the serial console
    said: list[str] = []
    bc = t.check_boot(VmRef(sid, "w"), timeout=30, guest_os="windows", progress=said.append)
    assert bc.ok and "Windows" in bc.detail and "VHI console" in bc.detail
    bc = t.check_boot(VmRef(sid, "w"), timeout=0.05, guest_os="linux", progress=said.append)
    assert bc.ok and "verify boot manually" in bc.detail and said          # progress was reported
    api.servers[sid]["status"] = "SHUTOFF"
    bc = t.check_boot(VmRef(sid, "w"), timeout=30, guest_os="windows")
    assert not bc.ok and "powered off" in bc.detail


def test_vhi_cleanup_treats_already_deleted_objects_as_done(tmp_path):
    """Seen live: a second failure cleanup crashed with a Cinder 404 on a volume the first cleanup
    had already deleted. Gone means cleaned up; the remaining objects are still removed."""
    t, api = _tgt(tmp_path)
    gone = t.create_disk("vm-disk00", 1 << 20, DiskHints(bus=DiskBus.SCSI))
    kept = t.create_disk("vm-disk01", 1 << 20, DiskHints(bus=DiskBus.SCSI))
    api.delete_volume(gone.id)
    created = {"target_disks": [{"id": r.id, "name": r.name, "size_bytes": r.size_bytes} for r in (gone, kept)],
               "target_vm": {"id": "srv-gone", "name": "vm"}}
    t.cleanup_on_failure(created)
    assert api.volumes == {}
    t.cleanup_on_failure(created)                     # repeat: still no error
