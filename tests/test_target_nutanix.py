"""Purpose: Nutanix AHV target: volume group per disk written over (fake) iSCSI into a local file "LUN",
VM created by cloning the volume-group disks, volume groups removed after a migration, replica deploy
by reference clone, cleanup safety.

Usage: pytest tests/test_target_nutanix.py
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

from vmigrate.config.loader import EngineConfig, MigrationConfig
from vmigrate.core.context import RunContext
from vmigrate.core.errors import ConfigError, SafetyGateError
from vmigrate.core.interfaces import DiskHints, VmRef
from vmigrate.engine import MigrationRunner, Phase, StateStore
from vmigrate.ir import CpuSpec, DiskBus, DiskSpec, Firmware, GuestOS, NicSpec, OsFamily, VmSpec
from vmigrate.providers import TARGETS
from vmigrate.providers.fake.provider import FakeSource
from vmigrate.providers.nutanix import target as T

from .conftest import fake_vm


class FakeNtx:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.vgs: dict[str, dict] = {"vg-foreign": {"name": "prod-vg", "disks": [], "clients": []}}
        self.vms: dict[str, dict] = {"vm-foreign": {"name": "db01", "description": "prod", "power_state": "on",
                                                    "vm_disk_info": []}}
        self.n = 0
        self.log: list[str] = []

    def _id(self, p):
        self.n += 1
        return f"{p}-{self.n}"
    def disk_path(self, ext): return self.root / ext
    def cluster_uuid(self): return "cl-1"
    def data_services_ip(self, cl): return "10.65.49.79"
    def containers(self): return [{"uuid": "ct-1", "name": "default-container", "free": 1 << 40}]
    def networks(self): return [{"uuid": "net-1", "name": "vm-network", "vlan": 0}]
    def create_volume_group(self, name, cl):
        assert name.startswith("vmigrate-")
        ext = self._id("vg")
        self.vgs[ext] = {"name": name, "disks": [], "clients": []}
        return ext
    def find_volume_group(self, name):
        hits = [k for k, v in self.vgs.items() if v["name"] == name]
        return hits[0] if len(hits) == 1 else None
    def volume_group(self, vg): return {"name": self.vgs[vg]["name"]} if vg in self.vgs else None
    def add_empty_disk(self, vg, size, cont, index=0):
        ext = self._id("vdisk")
        self.vgs[vg]["disks"].append({"ext": ext, "index": index, "size": size})
        with open(self.disk_path(ext), "wb") as f:
            f.truncate(size)
    def vg_disks(self, vg): return list(self.vgs[vg]["disks"])
    def attach_iscsi_client(self, vg, iqn): self.vgs[vg]["clients"].append(iqn)
    def detach_iscsi_client(self, vg, iqn):
        if vg in self.vgs:
            self.vgs[vg]["clients"] = []
    def delete_volume_group(self, vg):
        assert self.vgs[vg]["name"].startswith("vmigrate-")
        assert not self.vgs[vg]["clients"], "delete while an initiator is attached"
        self.log.append(f"delete-vg:{self.vgs[vg]['name']}")
        self.vgs.pop(vg)
    def create_vm(self, body):
        uuid = self._id("vm")
        info = []
        for d in body["vm_disks"]:
            src = d["vm_disk_clone"]["disk_address"]["vmdisk_uuid"]
            new = self._id("vmdisk")
            shutil.copyfile(self.disk_path(src), self.disk_path(new))      # the clone
            info.append({"is_cdrom": False, "disk_address": {**d["disk_address"], "vmdisk_uuid": new}})
        self.vms[uuid] = {"name": body["name"], "description": body["description"], "power_state": "off",
                          "vm_disk_info": info, "body": body}
        return uuid
    def vm(self, uuid): return self.vms.get(uuid)
    def vms_by_name(self, name): return [{"uuid": k, **v} for k, v in self.vms.items() if v["name"] == name]
    def power(self, uuid, t): self.vms[uuid]["power_state"] = "on" if t == "ON" else "off"
    def delete_vm(self, uuid): self.vms.pop(uuid)
    def attach_disk(self, uuid, spec): raise AssertionError("everything is attached at create")


class FakeIscsi:
    """'LUN 0 of the group's target' is the disk's backing file."""

    def __init__(self, api: FakeNtx):
        self.api = api

    def __call__(self, portal):
        api = self

        class S:
            def initiator_name(self): return "iqn.2004-10.com.ubuntu:worker"
            def login(self, vg_name, expected):
                vg = api.api.find_volume_group(vg_name)
                assert api.api.vgs[vg]["clients"], "login before the initiator was attached"
                d = api.api.vgs[vg]["disks"][0]
                return [(f"iqn.2010-06.com.nutanix:{vg_name}", 0, str(api.api.disk_path(d["ext"])))]
            def logout(self): pass
        return S()


def _tgt(tmp_path, ctx=None, **cfg):
    ctx = ctx or RunContext(dry_run=False, logger=logging.getLogger("ntx"), run_id="run-abcdef123456")
    api = FakeNtx(tmp_path / "luns")
    conf = {"type": "nutanix", "target_container": "default-container", "network_map": {"net0": "vm-network"},
            "poll_interval": 0, "boot_grace": 0, **cfg}
    t = T.NutanixTarget("ntx", conf, ctx, api=api, iscsi_factory=FakeIscsi(api), size_of=lambda p: Path(p).stat().st_size)
    return t, api


def _spec(**kw):
    base = dict(name="web01", source_id="vm-1", cpu=CpuSpec(sockets=2, cores=2), memory_bytes=4 << 30,
                disks=[DiskSpec(id="d0", bus=DiskBus.SCSI, size_bytes=1 << 20, order=0, is_boot=True)],
                nics=[NicSpec(id="n0", network="net0", mac="00:50:56:AA:BB:CC")], firmware=Firmware.UEFI,
                secure_boot=True, guest_os=GuestOS(OsFamily.LINUX))
    base.update(kw)
    return VmSpec(**base)


def test_pure_helpers():
    assert T.vg_name("RIS-Sainath disk00") == "vmigrate-disk-ris-sainath-disk00"
    assert T.round_mib(1) == 1 << 20
    from vmigrate.core.interfaces import TargetDiskRef
    d = [TargetDiskRef("vg-1", "a", 1 << 20, data={"disk_ext": "vd-1"}), TargetDiskRef("vg-2", "b", 2 << 20, data={"disk_ext": "vd-2"})]
    b = T.vm_body(_spec(), d, ["net-1"], macs=["00:50:56:AA:BB:CC"])
    assert b["vm_disks"][1]["vm_disk_clone"]["disk_address"]["vmdisk_uuid"] == "vd-2"
    assert b["boot"] == {"disk_address": {"device_bus": "scsi", "device_index": 0}, "uefi_boot": True, "secure_boot": True}
    assert b["machine_type"] == "Q35" and b["vm_nics"] == [{"network_uuid": "net-1", "mac_address": "00:50:56:aa:bb:cc"}]
    assert "vmigrate-disks=vg-1,vg-2" in b["description"] and b["num_vcpus"] == 2 and b["memory_mb"] == 4096
    vm = {"description": b["description"], "vm_disk_info": [
        {"disk_address": {"device_bus": "scsi", "device_index": 1, "vmdisk_uuid": "x2"}},
        {"disk_address": {"device_bus": "scsi", "device_index": 0, "vmdisk_uuid": "x1"}},
        {"is_cdrom": True, "disk_address": {"device_bus": "ide", "device_index": 0}}]}
    assert T.attached_refs(vm) == ["vg-1", "vg-2"]
    assert T.attached_refs({**vm, "description": ""}) == ["x1", "x2"]


def test_full_migration_to_nutanix(tmp_path):
    assert TARGETS["nutanix"][1] == "implemented"
    ctx = RunContext(dry_run=False, logger=logging.getLogger("ntx-e2e"), run_id="run-abcdef123456")
    src = FakeSource("src", {"type": "fake", "vms": [fake_vm(disks=3, size="3MiB")]}, ctx)
    t, api = _tgt(tmp_path, ctx, default_network="vm-network")
    assert t.check_connection() == []
    eng = EngineConfig(state_dir=tmp_path / "state", report_dir=tmp_path / "rep", chunk_size=1 << 20,
                       verify_samples=4, verify_sample_bytes=64 << 10, boot_timeout=5, max_parallel_disks=2,
                       verify_full=True)
    job = MigrationConfig(name="m", source="src", target="ntx", vm="RIS-Sainath", osmorph={"allow_manual": True},
                          target_options={"network_map": {}})
    st = MigrationRunner(job, eng, src, t, ctx, StateStore(eng.state_dir)).run()
    t.close()
    assert st.phase == Phase.DONE, st.errors
    assert all(d.verify["full"]["ok"] for d in st.disks.values())
    vm = next(v for v in api.vms.values() if v["name"] == "RIS-Sainath")
    assert vm["power_state"] == "on" and len(vm["vm_disk_info"]) == 3
    # the VM's cloned disks hold the copied data; the volume groups are gone
    src_reader_bytes = [api.disk_path(d["disk_address"]["vmdisk_uuid"]).read_bytes()[:4096] for d in vm["vm_disk_info"]]
    assert all(any(b) for b in src_reader_bytes)
    assert set(api.vgs) == {"vg-foreign"} and sum(x.startswith("delete-vg:") for x in api.log) == 3
    assert st.verification["boot"]["ok"]


def test_replica_deploy_clones_by_reference(tmp_path):
    t, api = _tgt(tmp_path)
    d = t.create_disk("rep-r00", 3 << 20, DiskHints(bus=DiskBus.SCSI, is_boot=True, order=0))
    assert t.find_disk("rep-r00").id == d.id
    with t.open_disk_writer(d) as w:
        assert w.prezeroed
        w.write(0, b"R" * 4096)
    with t.open_disk_writer(d, incremental=True) as w:
        assert not w.prezeroed
    clone = t.clone_disk(d, "deploy-0")
    assert not api.vgs[d.id]["clients"]                                  # released for cloning
    vm = t.create_vm(_spec(), clone, [])
    assert d.id in api.vgs                                               # the replica stays
    assert api.disk_path(api.vms[vm.id]["vm_disk_info"][0]["disk_address"]["vmdisk_uuid"]).read_bytes()[:4096] == b"R" * 4096
    assert t.list_attached_disks(vm) == [clone.id]
    t.cleanup_on_failure({"target_vm": {"id": vm.id, "name": vm.name},
                          "target_disks": [{"id": clone.id, "name": clone.name, "size_bytes": 1, "data": clone.data}]})
    assert vm.id not in api.vms and d.id in api.vgs                      # never the replica
    t.delete_disk(d)
    assert d.id not in api.vgs


def test_safety_checks_and_lists(tmp_path):
    t, api = _tgt(tmp_path)
    with pytest.raises(SafetyGateError):
        t.cleanup_on_failure({"target_vm": {"id": "vm-foreign", "name": "db01"}})
    with pytest.raises(SafetyGateError):
        t.attach_disk(VmRef("vm-foreign", "db01", "ntx"), t.create_disk("x", 1 << 20, DiskHints(bus=DiskBus.SCSI, is_boot=True, order=0)), 1)
    from vmigrate.core.interfaces import TargetDiskRef
    with pytest.raises(SafetyGateError):
        t.delete_disk(TargetDiskRef("vg-foreign", "p", 1, data={"vg": "vg-foreign"}))
    t2, _ = _tgt(tmp_path / "b", target_container="nope")
    assert any("not found" in p for p in t2.check_connection())
    assert t.list_networks() == [{"id": "vm-network", "name": "vm-network (VLAN 0)"}]
    with pytest.raises(ConfigError):
        t.create_disk("shared", 1 << 20, DiskHints(bus=DiskBus.SCSI, is_boot=False, order=1, shared=True))
    p = t.preflight(_spec(nics=[NicSpec(id="n", network="other")]))
    assert any("other" in x for x in p)
