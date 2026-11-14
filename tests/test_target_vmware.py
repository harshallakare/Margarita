"""Purpose: VMware target: holder VM per run, thin disks added in order, writes over NBD (the real NBD agent on
local files stands in for nbdkit-vddk), the holder turned into the VM, replica copy, cleanup safety, and the
VMware OSMorph strategies' plans.

Usage: pytest tests/test_target_vmware.py
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from vmigrate.config.loader import EngineConfig, MigrationConfig
from vmigrate.core.context import RunContext
from vmigrate.core.errors import ConfigError, SafetyGateError
from vmigrate.core.interfaces import DiskHints, TargetDiskRef, VmRef
from vmigrate.engine import MigrationRunner, Phase, StateStore
from vmigrate.ir import CpuSpec, DiskBus, DiskSpec, Firmware, GuestOS, NicSpec, OsFamily, VmSpec
from vmigrate.osmorph import MorphRequest, resolve
from vmigrate.osmorph import vmware as OV
from vmigrate.providers import TARGETS
from vmigrate.providers.fake.provider import FakeSource
from vmigrate.providers.vhi import nbd_agent
from vmigrate.providers.vhi.nbd import NbdClient
from vmigrate.providers.vmware import target as T
from vmigrate.providers.vmware.target_client import slot

from .conftest import fake_vm


class FakeVsphere:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.vms: dict[str, dict] = {"vm-9": {"name": "prod-db", "power": "poweredOn", "extra": {}, "disks": [],
                                              "tools": "guestToolsRunning"}}
        self.n = 0

    def _id(self, p):
        self.n += 1
        return f"{p}-{self.n}"
    def path(self, file): return self.root / file.replace("[", "").replace("] ", "_").replace("/", "_")
    def about(self): return {"name": "vCenter 8", "api": "8.0", "type": "VirtualCenter"}
    def check(self): return []
    def thumbprint(self): return "AA:BB"
    def networks(self): return [{"name": "PG-120", "type": "DistributedVirtualPortgroup"}, {"name": "VM Network", "type": "Network"}]
    def datastores(self): return [{"name": "ds1", "free": 1 << 40, "accessible": True}]
    def create_vm(self, name, *, controller, annotation, extra):
        m = self._id("vm")
        self.vms[m] = {"name": name, "power": "poweredOff", "extra": dict(extra), "disks": [], "controller": controller,
                       "tools": "guestToolsNotRunning"}
        return m
    def vm_info(self, m):
        v = self.vms.get(m)
        if v is None:
            return None
        return {"moref": m, "name": v["name"], "power": v["power"], "tools": v["tools"], "extra": dict(v["extra"]),
                "disks": sorted(v["disks"], key=lambda d: (d["bus"], d["unit"])), "vmx_dir": f"[ds1] {v['name']}/"}
    def managed_vms(self):
        return [{"moref": m, "name": v["name"], "extra": dict(v["extra"])} for m, v in self.vms.items()
                if v["extra"].get("vmigrate.managed") == "true"]
    def find_vms(self, name): return [{"moref": m, "name": v["name"]} for m, v in self.vms.items() if v["name"] == name]
    def add_disk(self, m, size, index, *, controller, existing_file=None):
        bus, unit = slot(index)
        key = 2000 + index
        file = existing_file or f"[ds1] {self.vms[m]['name']}/{self.vms[m]['name']}_{index}.vmdk"
        if not existing_file:
            with open(self.path(file), "wb") as f:
                f.truncate(size)
        d = {"uuid": self._id("uuid"), "key": key, "file": file, "capacity": size, "bus": bus, "unit": unit}
        self.vms[m]["disks"].append(d)
        return d
    def set_extra(self, m, extra): self.vms[m]["extra"].update(extra)
    def remove_disk(self, m, key, *, destroy):
        d = next(x for x in self.vms[m]["disks"] if x["key"] == key)
        self.vms[m]["disks"].remove(d)
        if destroy:
            self.path(d["file"]).unlink()
    def copy_disk(self, src, dst): shutil.copyfile(self.path(src), self.path(dst))
    def reconfigure(self, m, c):
        self.vms[m].update(name=c["name"], cfg=c)
        self.vms[m]["extra"].update(c["extra"])
    def power_on(self, m):
        self.vms[m]["power"] = "poweredOn"
        self.vms[m]["tools"] = "guestToolsRunning"
    def power_off(self, m): self.vms[m]["power"] = "poweredOff"
    def destroy(self, m):
        for d in self.vms[m]["disks"]:
            self.path(d["file"]).unlink(missing_ok=True)
        self.vms.pop(m)


def export_factory(api: FakeVsphere, opened: list):
    class Export:
        def __init__(self, **kw):
            self.file = api.path(kw["file_name"])
            opened.append(kw)
        uri = "nbd://?socket=/nonexistent"
        def client(self):
            p = subprocess.Popen([sys.executable, "-c", Path(nbd_agent.__file__).read_text(), str(self.file), "rw"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            return NbdClient.over_process(p)
        def close(self): pass
    return Export


def _tgt(tmp_path, ctx=None, **cfg):
    ctx = ctx or RunContext(dry_run=False, logger=logging.getLogger("vmw"), run_id="run-abcdef123456")
    api = FakeVsphere(tmp_path / "ds")
    opened: list = []
    conf = {"type": "vmware", "host": "vc", "username": "u", "password": "p", "target_datastore": "ds1",
            "network_map": {"net0": "PG-120"}, "poll_interval": 0, "boot_grace": 0, **cfg}
    return T.VmwareTarget("vmw", conf, ctx, client=api, export_factory=export_factory(api, opened)), api, opened


def _spec(**kw):
    base = dict(name="web01", source_id="vm-1", cpu=CpuSpec(sockets=2, cores=2), memory_bytes=4 << 30,
                disks=[DiskSpec(id="d0", bus=DiskBus.SCSI, size_bytes=1 << 20, order=0, is_boot=True,
                                controller="virtio-scsi")],
                nics=[NicSpec(id="n0", network="net0", mac="52:54:00:aa:bb:cc", model="virtio")],
                firmware=Firmware.UEFI, secure_boot=True, guest_os=GuestOS(OsFamily.LINUX, distro="ubuntu"))
    base.update(kw)
    return VmSpec(**base)


def test_pure_helpers():
    assert slot(0) == (0, 0) and slot(6) == (0, 6) and slot(7) == (0, 8) and slot(15) == (1, 0)
    with pytest.raises(Exception):
        slot(60)
    s = T.vm_settings(_spec(), {"network_map": {"net0": "PG-120"}, "preserve_mac": True}, 2000)
    assert s["guest_id"] == "ubuntu64Guest" and s["firmware"] == "efi" and s["secure_boot"]
    assert s["nics"] == [{"network": "PG-120", "adapter": "vmxnet3", "mac": None}]       # non-VMware MAC dropped
    w = T.vm_settings(_spec(guest_os=GuestOS(OsFamily.WINDOWS), nics=[NicSpec(id="n", network="x", mac="00:50:56:01:02:03")]),
                      {"default_network": "VM Network", "preserve_mac": True}, 2000)
    assert w["guest_id"] == "windows9Server64Guest" and w["nics"][0] == {"network": "VM Network", "adapter": "e1000e",
                                                                        "mac": "00:50:56:01:02:03"}
    assert w["num_cpu"] == 4 and w["cores_per_socket"] == 2 and w["extra"]["vmigrate.holder"] == ""
    with pytest.raises(ConfigError):
        T.vm_settings(_spec(), {}, 1)
    with pytest.raises(ConfigError):
        T.VmwareTarget("v", {"scsi_controller": "buslogic"}, RunContext(dry_run=True, logger=logging.getLogger("x")))


def test_full_migration_to_vmware(tmp_path):
    assert TARGETS["vmware"][1] == "implemented"
    ctx = RunContext(dry_run=False, logger=logging.getLogger("vmw-e2e"), run_id="run-abcdef123456")
    src = FakeSource("src", {"type": "fake", "vms": [fake_vm(disks=3, size="3MiB")]}, ctx)
    t, api, opened = _tgt(tmp_path, ctx, default_network="VM Network")
    eng = EngineConfig(state_dir=tmp_path / "state", report_dir=tmp_path / "rep", chunk_size=1 << 20,
                       verify_samples=4, verify_sample_bytes=64 << 10, boot_timeout=5, max_parallel_disks=2,
                       verify_full=True)
    job = MigrationConfig(name="m", source="src", target="vmw", vm="RIS-Sainath", osmorph={"allow_manual": True},
                          target_options={"network_map": {}})
    st = MigrationRunner(job, eng, src, t, ctx, StateStore(eng.state_dir)).run()
    t.close()
    assert st.phase == Phase.DONE, st.errors
    assert all(d.verify["full"]["ok"] for d in st.disks.values())
    vm = next(v for v in api.vms.values() if v["name"] == "RIS-Sainath")
    assert vm["power"] == "poweredOn" and len(vm["disks"]) == 3 and vm["controller"] == "lsilogic-sas"
    assert vm["extra"]["vmigrate.holder"] == "" and vm["extra"]["vmigrate.managed"] == "true"
    assert all(k["vm_moref"] and k["file_name"].startswith("[ds1]") and k["thumbprint"] == "AA:BB" for k in opened)
    assert st.verification["boot"]["ok"] and "VMware Tools" in st.verification["boot"]["detail"]


def test_replica_copy_find_and_delete(tmp_path):
    t, api, _ = _tgt(tmp_path)
    d = t.create_disk("rep-r00", 3 << 20, DiskHints(bus=DiskBus.SCSI, is_boot=True, order=0))
    assert t.find_disk("rep-r00").id == d.id
    with t.open_disk_writer(d) as w:
        assert w.prezeroed
        w.write(0, b"V" * 4096)
    t2, _, _ = _tgt(tmp_path, RunContext(dry_run=False, logger=logging.getLogger("dep"), run_id="run-deploy0000001"))
    t2._client = api
    clone = t2.clone_disk(d, "deploy-0")
    assert clone.data["vm"] != d.data["vm"]
    assert api.path(clone.data["file"]).read_bytes()[:4096] == b"V" * 4096
    vm = t2.create_vm(_spec(), clone, [])
    assert t2.list_attached_disks(vm) == [clone.id]
    t.delete_disk(d)
    assert d.data["vm"] not in api.vms                         # empty holder removed


def test_safety(tmp_path):
    t, api, _ = _tgt(tmp_path)
    with pytest.raises(SafetyGateError):
        t.cleanup_on_failure({"target_vm": {"id": "vm-9", "name": "prod-db"}})
    with pytest.raises(SafetyGateError):
        t.delete_disk(TargetDiskRef("u", "x", 1, data={"vm": "vm-9", "key": 1, "file": "f"}))
    d = t.create_disk("m-disk00", 1 << 20, DiskHints(bus=DiskBus.SCSI, is_boot=True, order=0))
    api.vms[d.data["vm"]]["power"] = "poweredOn"
    with pytest.raises(SafetyGateError):
        t.open_disk_writer(d)
    t.cleanup_on_failure({"target_disks": [{"id": d.id, "name": d.name, "size_bytes": 1, "data": d.data}]})
    assert set(api.vms) == {"vm-9"}
    t.cleanup_on_failure({"target_disks": [{"id": d.id, "name": d.name, "size_bytes": 1, "data": d.data}]})
    assert t.find_vm("prod-db") == VmRef("vm-9", "prod-db", "vmw")
    assert [n["id"] for n in t.list_networks()] == ["PG-120", "VM Network"]
    assert any("lan-x" in p for p in t.preflight(_spec(nics=[NicSpec(id="n", network="lan-x")])))


def test_vmware_osmorph_plans():
    linux = _spec()
    req = MorphRequest(linux, "vmware", DiskBus.SCSI, [], {"vmware_controller": "pvscsi"})
    names = [s.name for s in resolve(OsFamily.LINUX, "vmware").plan(req)]
    assert "inject-pvscsi-initramfs" in names and "remove-hypervisor-tools" not in names
    fb = next(s for s in resolve(OsFamily.LINUX, "vmware").plan(req) if s.name == "firstboot-scripts")
    assert "10-open-vm-tools" in fb.description
    win = _spec(guest_os=GuestOS(OsFamily.WINDOWS))
    steps = OV.WindowsVmwareStrategy(which=lambda b: None).plan(MorphRequest(win, "vmware", DiskBus.SCSI, [], {}))
    assert steps[0].name == "enable-boot-storage-driver" and steps[0].required and steps[0].run is None
    assert "LSI_SAS" in OV.reg_file(["LSI_SAS"]) and '"Start"=dword:00000000' in OV.reg_file(["LSI_SAS"])
    already = _spec(guest_os=GuestOS(OsFamily.WINDOWS),
                    disks=[DiskSpec(id="d0", bus=DiskBus.SCSI, size_bytes=1, order=0, is_boot=True, controller="lsilogic-sas")])
    assert [s.name for s in OV.WindowsVmwareStrategy().plan(MorphRequest(already, "vmware", DiskBus.SCSI, [], {}))] == ["vmware-tools"]
