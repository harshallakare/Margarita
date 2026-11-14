"""Purpose: Proxmox VE target: holder VM per run, disk allocation, writes through the REAL NBD agent
(run locally against per-volume files in place of SSH), VM configuration, replica clone, cleanup safety.

Usage: pytest tests/test_target_proxmox.py
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from vmigrate.config.loader import EngineConfig, MigrationConfig
from vmigrate.core.context import RunContext
from vmigrate.core.errors import ConfigError, ProviderError, SafetyGateError
from vmigrate.core.interfaces import DiskHints, TargetDiskRef, VmRef
from vmigrate.engine import MigrationRunner, Phase, StateStore
from vmigrate.ir import CpuSpec, DiskBus, DiskSpec, Firmware, GuestOS, NicSpec, OsFamily, VmSpec
from vmigrate.providers import TARGETS
from vmigrate.providers.fake.provider import FakeSource
from vmigrate.providers.proxmox import target as T

from .conftest import fake_vm


class FakePve:
    def __init__(self, root: Path, storage_type: str = "lvmthin"):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.vms: dict[int, dict] = {500: {"name": "someone-else", "node": "pve1", "cfg": {"tags": "prod"}, "status": "stopped"}}
        self.vols: dict[str, dict] = {}
        self.stype = storage_type
        self.next = 900
        self.started: list[int] = []
        self.agent = True

    def path(self, volid): return self.root / volid.replace(":", "_")
    def version(self): return {"version": "8.2"}
    def nodes(self): return [{"node": "pve1", "status": "online"}, {"node": "pve2", "status": "offline"}]
    def storage(self, s): return {"storage": s, "type": self.stype}
    def node_storages(self, node):
        return [{"storage": "local-lvm", "type": self.stype, "content": "images,rootdir"},
                {"storage": "local", "type": "dir", "content": "iso,backup"}]
    def node_networks(self, node): return [{"iface": "vmbr0", "type": "bridge"}, {"iface": "eno1", "type": "eth"}]
    def storage_status(self, node, s): return {"avail": 1 << 40}
    def next_vmid(self):
        self.next += 1
        return self.next
    def all_vms(self): return [{"vmid": k, "name": v["name"], "node": v["node"], "type": "qemu"} for k, v in self.vms.items()]
    def config(self, node, vmid): return dict(self.vms[vmid]["cfg"])
    def create_vm(self, node, vmid, **cfg):
        assert vmid not in self.vms
        self.vms[vmid] = {"name": cfg["name"], "node": node, "cfg": dict(cfg), "status": "stopped"}
    def set_config(self, node, vmid, **cfg):
        self.vms[vmid]["cfg"].update(cfg)
        if "name" in cfg:
            self.vms[vmid]["name"] = cfg["name"]
    def alloc_disk(self, node, storage, vmid, filename, size_kib, fmt="raw"):
        volid = f"{storage}:{filename}"
        assert re.match(rf"^vm-{vmid}-", filename)
        self.vols[volid] = {"vmid": vmid, "size": size_kib * 1024}
        with open(self.path(volid), "wb") as f:
            f.truncate(size_kib * 1024)
        return volid
    def get(self, path, **params):
        return [{"volid": k, "vmid": v["vmid"], "size": v["size"]} for k, v in self.vols.items()]
    def content(self, node, storage, vmid):
        return [{"volid": k} for k, v in self.vols.items() if v["vmid"] == vmid]
    def delete_volume(self, node, storage, volid):
        self.vols.pop(volid)
        self.path(volid).unlink()
    def delete_vm(self, node, vmid):
        for k in [k for k, v in self.vols.items() if v["vmid"] == vmid]:
            self.delete_volume(node, "x", k)
        self.vms.pop(vmid)
    def start(self, node, vmid):
        self.vms[vmid]["status"] = "running"
        self.started.append(vmid)
    def status(self, node, vmid): return self.vms[vmid]["status"]
    def shutdown(self, node, vmid, force, timeout): self.vms[vmid]["status"] = "stopped"
    def agent_ping(self, node, vmid): return self.agent


class LocalNode:
    """Stands in for SshNode on the target node: `pvesm path` maps to local files; spawn runs the agent."""

    def __init__(self, api: FakePve):
        self.api = api
        self.cmds: list[list[str]] = []

    def run(self, argv, *, check=True, timeout=None):
        self.cmds.append(argv)
        if argv[:2] == ["pvesm", "path"]:
            return str(self.api.path(argv[2])) + "\n"
        if argv[:2] == ["python3", "-c"]:
            return "ok\n"
        if argv[0] == "qemu-img":
            shutil.copyfile(argv[-2], argv[-1])
            return ""
        raise AssertionError(argv)

    def spawn(self, argv):
        assert argv[:2] == ["python3", "-c"]
        return subprocess.Popen([sys.executable, "-c", argv[2], argv[3], argv[4]], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)

    def close(self): pass


def _tgt(tmp_path, ctx=None, stype="lvmthin", **cfg):
    ctx = ctx or RunContext(dry_run=False, logger=logging.getLogger("pve"), run_id="run-abcdef123456")
    api = FakePve(tmp_path / "vols", stype)
    node = LocalNode(api)
    conf = {"type": "proxmox", "url": "https://pve1:8006", "token_id": "t@pve!x", "target_storage": "local-lvm",
            "network_map": {"net0": "vmbr0"}, "poll_interval": 0, "ssh_private_key": "k", "ssh_known_hosts": "h", **cfg}
    return T.ProxmoxTarget("pve", conf, ctx, api=api, shell_factory=lambda n: node), api, node


def _spec(**kw):
    base = dict(name="web01", source_id="vm-1", cpu=CpuSpec(sockets=1, cores=4), memory_bytes=8 << 30,
                disks=[DiskSpec(id="d0", bus=DiskBus.SCSI, size_bytes=10 << 30, order=0, is_boot=True)],
                nics=[NicSpec(id="n0", network="net0", mac="00:50:56:aa:bb:cc")],
                firmware=Firmware.BIOS, guest_os=GuestOS(OsFamily.LINUX))
    base.update(kw)
    return VmSpec(**base)


def test_pure_helpers():
    assert T.volume_filename(901, "RIS-Sainath disk 00", "lvmthin") == "vm-901-vmigrate-ris-sainath-disk-00"
    assert T.volume_filename(901, "web", "dir").endswith(".raw")
    assert T.size_kib(1) == 1024 and T.size_kib((5 << 20) + 1) == 6 * 1024
    cfg = {"scsi1": "local-lvm:vm-1-b,size=1G", "scsi0": "local-lvm:vm-1-a", "ide2": "local:iso/x.iso,media=cdrom",
           "virtio0": "local-lvm:vm-1-c", "net0": "virtio,bridge=vmbr0", "scsi10": "local-lvm:vm-1-d"}
    assert T.attached_volids(cfg) == ["local-lvm:vm-1-a", "local-lvm:vm-1-b", "local-lvm:vm-1-d", "local-lvm:vm-1-c"]
    assert T.net_value("vmbr1:120", "aa:bb:cc:dd:ee:ff") == "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr1,tag=120"
    c = T.vm_config(_spec(), ["s:a", "s:b"], {"network_map": {"net0": "vmbr0"}, "preserve_mac": True},
                    storage="s", bus=DiskBus.SCSI)
    assert c["scsi0"] == "s:a,discard=on" and c["boot"] == "order=scsi0" and c["ostype"] == "l26"
    assert c["net0"].startswith("virtio=00:50:56:AA:BB:CC") and c["memory"] == 8192 and c["tags"] == "vmigrate"
    win = _spec(firmware=Firmware.UEFI, secure_boot=True, guest_os=GuestOS(OsFamily.WINDOWS, version="2022"))
    c = T.vm_config(win, ["s:a"], {"default_bridge": "vmbr0"}, storage="s", bus=DiskBus.SCSI)
    assert c["bios"] == "ovmf" and c["machine"] == "q35" and "pre-enrolled-keys=1" in c["efidisk0"]
    assert c["tpmstate0"] == "s:1,version=v2.0" and c["ostype"] == "win11"
    with pytest.raises(ConfigError):
        T.vm_config(_spec(), ["s:a"], {}, storage="s", bus=DiskBus.SCSI)       # unmapped network


def test_full_migration_to_proxmox(tmp_path):
    assert TARGETS["proxmox"][1] == "implemented"
    ctx = RunContext(dry_run=False, logger=logging.getLogger("pve-e2e"), run_id="run-abcdef123456")
    src = FakeSource("src", {"type": "fake", "vms": [fake_vm(disks=3, size="3MiB")]}, ctx)
    t, api, node = _tgt(tmp_path, ctx)
    assert t.preflight() == []
    eng = EngineConfig(state_dir=tmp_path / "state", report_dir=tmp_path / "rep", chunk_size=1 << 20,
                       verify_samples=4, verify_sample_bytes=64 << 10, boot_timeout=5, max_parallel_disks=2,
                       verify_full=True)
    job = MigrationConfig(name="m", source="src", target="pve", vm="RIS-Sainath", osmorph={"allow_manual": True},
                          target_options={"network_map": {}, "default_bridge": "vmbr0"})
    st = MigrationRunner(job, eng, src, t, ctx, StateStore(eng.state_dir)).run()
    t.close()
    assert st.phase == Phase.DONE, st.errors
    assert all(d.verify["full"]["ok"] for d in st.disks.values())
    (vmid,) = api.started
    vm = api.vms[vmid]
    assert vm["name"] == "RIS-Sainath" and vm["status"] == "running"
    assert "vmigrate-holder" not in vm["cfg"]["tags"]                       # holder became the real VM
    assert [k for k in vm["cfg"] if re.match(r"scsi\d+$", k)] == ["scsi0", "scsi1", "scsi2"]
    assert all(v["vmid"] == vmid for v in api.vols.values()) and len(api.vols) == 3
    assert st.verification["boot"]["ok"] and "guest agent" in st.verification["boot"]["detail"]


def test_replica_style_writes_clone_and_delete(tmp_path):
    t, api, node = _tgt(tmp_path)
    d = t.create_disk("rep-r00", 3 << 20, DiskHints(bus=DiskBus.SCSI, is_boot=True, order=0))
    holder = d.data["vmid"]
    assert api.vms[holder]["name"] == "vmigrate-holder-run-abcdef12" and t.find_disk("rep-r00").id == d.id
    with t.open_disk_writer(d) as w:
        assert w.prezeroed
        w.write(1 << 20, b"A" * 4096)
    with t.open_disk_writer(d, incremental=True) as w:
        assert not w.prezeroed
        w.write(0, b"B" * 512)
    clone = t.clone_disk(d, "deploy-0")
    assert api.path(clone.id).read_bytes() == api.path(d.id).read_bytes()
    with t.open_disk_reader(d) as r:
        assert r.read(0, 512) == b"B" * 512 and r.read(1 << 20, 4096) == b"A" * 4096
    t.delete_disk(clone)
    t.delete_disk(d)
    assert holder not in api.vms                                   # empty holder removed with its last disk


def test_safety_and_cleanup(tmp_path):
    t, api, _ = _tgt(tmp_path)
    foreign = TargetDiskRef("local-lvm:vm-500-disk-0", "x", 1 << 20, data={"vmid": 500})
    with pytest.raises(SafetyGateError):
        t.delete_disk(foreign)
    with pytest.raises(SafetyGateError):
        t.attach_disk(VmRef("500", "someone-else", "pve"), foreign, 1)
    with pytest.raises(SafetyGateError):
        t.cleanup_on_failure({"target_vm": {"id": "500", "name": "someone-else"}})
    d = t.create_disk("m-disk00", 2 << 20, DiskHints(bus=DiskBus.SCSI, is_boot=True, order=0))
    t.cleanup_on_failure({"target_disks": [{"id": d.id, "name": d.name, "size_bytes": d.size_bytes, "data": d.data}]})
    assert d.data["vmid"] not in api.vms and 500 in api.vms and not api.vols
    t.cleanup_on_failure({"target_disks": [{"id": d.id, "name": d.name, "size_bytes": 1, "data": d.data}]})   # again: no-op


def test_checks_and_lists(tmp_path):
    t, api, _ = _tgt(tmp_path, target_storage="local")
    assert any("does not hold VM images" in p for p in t.check_connection())
    t2, api2, _ = _tgt(tmp_path / "b", stype="iscsi")
    assert any("not supported" in p for p in t2.check_connection())
    with pytest.raises(ConfigError):
        t2.create_disk("x", 1 << 20, DiskHints(bus=DiskBus.SCSI, is_boot=True, order=0))
    t3, _, _ = _tgt(tmp_path / "c")
    assert t3.list_networks() == [{"id": "vmbr0", "name": "vmbr0"}]
    assert [s["id"] for s in t3.list_storage()] == ["local-lvm"]
    p = t3.preflight(_spec(nics=[NicSpec(id="n", network="lan-x")]))
    assert any("lan-x" in x for x in p)
    t4, api4, _ = _tgt(tmp_path / "d", stype="lvm")
    d = t4.create_disk("thick", 1 << 20, DiskHints(bus=DiskBus.SCSI, is_boot=True, order=0))
    with t4.open_disk_writer(d) as w:
        assert not w.prezeroed                                     # thick LVM may hold old data


def test_boot_check_without_agent_and_dry_run(tmp_path):
    t, api, _ = _tgt(tmp_path, boot_grace=0)
    api.agent = False
    api.vms[700] = {"name": "x", "node": "pve1", "cfg": {"tags": "vmigrate"}, "status": "running"}
    r = t.check_boot(VmRef("700", "x", "pve"), timeout=5)
    assert r.ok and "Proxmox console" in r.detail
    dry = RunContext(dry_run=True, logger=logging.getLogger("d"), run_id="r")
    td, apid, _ = _tgt(tmp_path / "dry", dry)
    with pytest.raises(Exception):
        td.create_disk("x", 1 << 20, DiskHints(bus=DiskBus.SCSI, is_boot=True, order=0))
    assert set(apid.vms) == {500}
