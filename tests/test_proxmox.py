"""Purpose: Proxmox source tests without a cluster: config parsing + refusals, the storage-content
cross-check, per-storage snapshot exposure plans, SSH hygiene, NodeExport's full sequence against a
fake node whose socket forward is a real in-test NBD server, and engine runs (migration, refusals,
Ceph RBD diff replica, compare-mode replica) through ProxmoxSource with a fake PVE API.

Usage: pytest tests/test_proxmox.py
"""

from __future__ import annotations

import json
import logging
import os
import stat

import pytest

from vmigrate.config.loader import EngineConfig, MigrationConfig
from vmigrate.core.context import RunContext
from vmigrate.core.errors import ConfigError, DiskCountMismatch, SafetyGateError
from vmigrate.engine import MigrationRunner, StateStore
from vmigrate.engine.dag import DagRunner, MemoryTaskStore
from vmigrate.engine.replica import MemoryReplicaStore, ReplicaRunner, sync_dag
from vmigrate.engine.safety import check_disk_counts
from vmigrate.ir import DiskBus, Firmware, OsFamily
from vmigrate.providers.fake.provider import FakeTarget, PatternDisk
from vmigrate.providers.proxmox import mapping
from vmigrate.providers.proxmox.node import NodeExport, SshNode, qemu_nbd_argv
from vmigrate.providers.proxmox.source import ProxmoxSource

from .test_vmware import _nbd_server

SIZE = 2 << 20
N = 13


def cfg(n: int = N, storage: str = "ceph-vm", **extra) -> dict:
    c = {"name": "RIS-Sainath", "cores": 2, "sockets": 2, "memory": "current=8192", "bios": "ovmf", "ostype": "l26",
         "scsihw": "virtio-scsi-single", "agent": "enabled=1", "boot": "order=scsi0;ide2;net0",
         "efidisk0": f"{storage}:vm-100-disk-efi,efitype=4m,pre-enrolled-keys=1,size=4M",
         "ide2": "local:iso/rhel9.iso,media=cdrom", "ide3": f"{storage}:vm-100-cloudinit,media=cdrom",
         "unused0": f"{storage}:vm-100-disk-99",
         "net0": "virtio=BC:24:11:AA:BB:CC,bridge=vmbr0,tag=20,firewall=1", "net1": "e1000=BC:24:11:00:00:01,bridge=vmbr1"}
    for i in range(n):
        c[f"scsi{i}"] = f"{storage}:vm-100-disk-{i},iothread=1,size=2M"
    c.update(extra)
    return c


def content(n: int = N, storage: str = "ceph-vm", extra: int = 0) -> list[dict]:
    rows = [{"volid": f"{storage}:vm-100-disk-{i}", "vmid": 100, "size": SIZE, "content": "images"} for i in range(n + extra)]
    rows += [{"volid": f"{storage}:vm-100-disk-99", "vmid": 100, "size": SIZE, "content": "images"},     # unused
             {"volid": f"{storage}:vm-100-disk-efi", "vmid": 100, "size": 4 << 20, "content": "images"},  # efidisk
             {"volid": f"{storage}:vm-100-cloudinit", "vmid": 100, "size": 4 << 20, "content": "images"}]
    return rows


VM = {"vmid": 100, "name": "RIS-Sainath", "node": "pve1", "type": "qemu"}


# ---------------------------------------------------------------------- mapping

def test_mapping_13_disks_and_hardware():
    sizes = {c["volid"]: c["size"] for c in content()}
    spec = mapping.to_vmspec(VM, cfg(), sizes, {"id": "rhel", "version-id": "9.3"})
    assert len(spec.disks) == 13 and spec.boot_disk().label == "scsi0" and spec.ordered_disks()[12].label == "scsi12"
    assert {d.controller for d in spec.disks} == {"virtio-scsi"} and spec.disks[0].bus == DiskBus.SCSI
    assert spec.firmware == Firmware.UEFI and spec.secure_boot and spec.memory_bytes == 8 << 30
    assert spec.cpu.total == 4 and spec.guest_os.family == OsFamily.LINUX and spec.guest_os.distro == "rhel"
    assert [(n.model, n.mac, n.network) for n in spec.nics] == [("virtio", "BC:24:11:AA:BB:CC", "vmbr0/vlan20"),
                                                                 ("e1000", "BC:24:11:00:00:01", "vmbr1")]
    assert check_disk_counts(spec, mapping.crosscheck(cfg(), content(), 100))
    with pytest.raises(DiskCountMismatch):     # storage holds a disk the config enumeration did not return
        check_disk_counts(spec, mapping.crosscheck(cfg(), content(extra=1), 100))


def test_mapping_parsing_and_refusals():
    assert mapping.parse_size("1.5G") == 3 << 29 and mapping.parse_size("512M") == 512 << 20
    assert mapping.parse_props("local:vm-1-disk-0,size=8G")[1]["size"] == "8G"
    c = cfg(2, scsihw="pvscsi")
    c["virtio0"] = "local-lvm:vm-100-disk-7,size=2M"
    spec = mapping.to_vmspec(VM, c)
    assert [d.controller for d in spec.ordered_disks()] == ["pvscsi", "pvscsi", "virtio-blk"]
    with pytest.raises(mapping.ProxmoxMappingError, match="pass-through"):
        mapping.config_disks(cfg(1, scsi5="/dev/disk/by-id/ata-SAMSUNG,size=1T"))
    assert mapping.to_vmspec(VM, cfg(1, bios="seabios", boot="cdn", bootdisk="scsi0")).firmware == Firmware.BIOS


def test_expose_plans_per_storage():
    tag = "vmigrate-run1-scsi0"
    p = mapping.expose_plan("local-lvm:vm-100-disk-0", "lvmthin", "/dev/pve/vm-100-disk-0", "raw", "vmigrate-run1", tag)
    assert p["setup"] == [["lvchange", "-ay", "-Ky", "pve/snap_vm-100-disk-0_vmigrate-run1"]]
    assert p["nbd_target"] == "/dev/pve/snap_vm-100-disk-0_vmigrate-run1" and p["teardown"][0][:2] == ["lvchange", "-an"]
    p = mapping.expose_plan("local-zfs:vm-100-disk-0", "zfspool", "/dev/zvol/rpool/data/vm-100-disk-0", "raw",
                            "vmigrate-run1", tag)
    assert p["setup"][0] == ["zfs", "clone", "-o", "readonly=on", "rpool/data/vm-100-disk-0@vmigrate-run1", f"rpool/{tag}"]
    assert p["teardown"] == [["zfs", "destroy", f"rpool/{tag}"]]
    p = mapping.expose_plan("ceph:vm-100-disk-0", "rbd", "rbd:vmpool/vm-100-disk-0:conf=/etc/pve/ceph.conf:id=admin",
                            "raw", "vmigrate-run1", tag)
    assert p["nbd_target"] == "rbd:vmpool/vm-100-disk-0@vmigrate-run1:conf=/etc/pve/ceph.conf:id=admin"
    p = mapping.expose_plan("nfs:100/vm-100-disk-0.qcow2", "nfs", "/mnt/pve/nfs/images/100/vm-100-disk-0.qcow2",
                            "qcow2", "vmigrate-run1", tag)
    assert "--load-snapshot=snapshot.name=vmigrate-run1" in p["nbd_extra"] and "--force-share" in p["nbd_extra"]
    with pytest.raises(mapping.ProxmoxMappingError, match="cannot be snapshotted"):
        mapping.expose_plan("d:100/x.raw", "dir", "/var/lib/vz/images/100/x.raw", "raw", "vmigrate-run1", tag)
    with pytest.raises(mapping.ProxmoxMappingError, match="does not support snapshots"):
        mapping.expose_plan("thick:vm-100-disk-0", "lvm", "/dev/vg/vm-100-disk-0", "raw", "vmigrate-run1", tag)
    with pytest.raises(mapping.ProxmoxMappingError, match="unsafe"):
        mapping.expose_plan("x", "rbd", "rbd:p/i;rm -rf /", "raw", "vmigrate-run1", tag)
    with pytest.raises(mapping.ProxmoxMappingError, match="vmigrate-"):
        mapping.expose_plan("x", "zfspool", "/dev/zvol/rpool/x", "raw", "vmigrate-run1", "tmp-clone")
    assert "--read-only" in qemu_nbd_argv("/s", "/p", "/dev/x", "raw", [])


def test_ssh_node_pins_host_keys_and_protects_the_key():
    with pytest.raises(ConfigError, match="known_hosts"):
        SshNode("10.0.0.11", "root", "KEY", "")
    n = SshNode("10.0.0.11", "root", "-----BEGIN KEY-----\nabc\n-----END KEY-----", "10.0.0.11 ssh-ed25519 AAAA")
    argv = n.base_argv()
    assert "StrictHostKeyChecking=yes" in argv and "BatchMode=yes" in argv
    assert stat.S_IMODE(os.stat(n.key).st_mode) == 0o600
    n.close()
    assert not n.key.exists()


class FakeShell:
    """Records node commands; 'forwards' the socket by starting a real NBD server at the local path."""

    def __init__(self, data: bytes = b"", fail_on: str | None = None, rbd: dict | None = None, storage: str = "rbd"):
        self.data, self.fail_on, self.rbd, self.storage = data, fail_on, rbd or {}, storage
        self.cmds: list[list[str]] = []

    def run(self, argv, check=True, timeout=None):
        self.cmds.append(argv)
        if self.fail_on and argv[0] == self.fail_on:
            from vmigrate.core.errors import ProviderError
            raise ProviderError(f"{argv[0]} failed")
        if argv[0] == "sh" and "command -v qemu-nbd" in argv[-1]:
            return "ok\n"
        if argv[:2] == ["pvesm", "path"]:
            vol = argv[2].split(":", 1)[1]
            if self.storage == "lvmthin":
                return f"/dev/pve/{vol}\n"
            return f"rbd:vmpool/{vol}:conf=/etc/pve/ceph.conf:id=admin\n"
        if argv[:2] == ["rbd", "diff"]:
            spec = next(a for a in argv[2:] if "@" in a and not a.startswith("-"))
            return json.dumps(self.rbd.get(spec.split("@")[0].split("/")[-1], []))
        return ""

    def forward(self, lsock, rsock):
        _nbd_server(lsock, self.data)

        class P:
            stderr = None

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0
        return P()


def test_node_export_full_sequence_and_teardown():
    data = PatternDisk("pve", 3 << 20).read(0, 3 << 20)
    sh = FakeShell(data)
    plan = mapping.expose_plan("local-zfs:vm-100-disk-0", "zfspool", "/dev/zvol/rpool/data/vm-100-disk-0", "raw",
                               "vmigrate-r1", "vmigrate-r1-scsi0")
    with NodeExport(sh, plan, tag="vmigrate-r1-scsi0") as r:
        assert r.size == len(data) and r.read(4096, 70000) == data[4096:74096]
    names = [c[0] for c in sh.cmds]
    assert names[:4] == ["mkdir", "zfs", "udevadm", "qemu-nbd"] and ["zfs", "destroy", "rpool/vmigrate-r1-scsi0"] in sh.cmds
    sh2 = FakeShell(data, fail_on="udevadm")                    # clone made, settle fails -> clone still destroyed
    with pytest.raises(Exception, match="udevadm"):
        NodeExport(sh2, plan, tag="vmigrate-r1-scsi0")
    assert ["zfs", "destroy", "rpool/vmigrate-r1-scsi0"] in sh2.cmds


# ---------------------------------------------------------------------- engine runs

class FakePve:
    def __init__(self, storage_type="rbd", n=N, agent=True):
        self.c = cfg(n) if agent else cfg(n, agent="0")
        self.storage_type, self.n, self.gen = storage_type, n, 0
        self.snaps: dict[str, int] = {}
        self.deleted: list[str] = []
        self.pend: list = []

    def version(self):
        return {"version": "8.2"}

    def vms(self):
        return [VM]

    def config(self, node, vmid):
        return self.c

    def pending(self, node, vmid):
        return self.pend

    def content(self, node, storage, vmid):
        return content(self.n)

    def storage(self, sid):
        return {"type": self.storage_type}

    def agent_osinfo(self, node, vmid):
        return {"id": "rhel", "version-id": "9.3"}

    def snapshots(self, node, vmid):
        return [{"name": s} for s in self.snaps]

    def get(self, path):
        return self.c

    def create_snapshot(self, node, vmid, name, vmstate=False):
        self.snaps[name] = self.gen

    def delete_snapshot(self, node, vmid, name):
        assert name.startswith("vmigrate-")
        self.deleted.append(name)
        self.snaps.pop(name, None)


def _src(api, shell, ctx):
    def export(sh, plan, tag, snap, disk):
        i = int(disk.source_handle["volid"].rsplit("-", 1)[1])
        assert snap.id in plan["nbd_target"]                            # always the snapshot, never the live image
        return PatternDisk(f"RIS-Sainath:{i}", SIZE, generation=api.snaps[snap.id])
    return ProxmoxSource("pve", {"type": "proxmox", "url": "https://pve1:8006", "token_id": "t@pve!x",
                                 "ssh_private_key": "k", "ssh_known_hosts": "h"}, ctx, api=api,
                         shell_factory=lambda node: shell, export_factory=export)


def _env(tmp_path, api, shell=None):
    ctx = RunContext(dry_run=False, logger=logging.getLogger("t"))
    tgt = FakeTarget("tgt", {"staging_dir": str(tmp_path / "tgt"), "persist": True}, ctx)
    e = EngineConfig(state_dir=tmp_path / "s", report_dir=tmp_path / "r", chunk_size=1 << 20, checkpoint_every=2 << 20,
                     verify_samples=4, verify_sample_bytes=64 << 10)
    return ctx, _src(api, shell or FakeShell(), ctx), tgt, e


def test_migration_virtio_guest_needs_no_injection(tmp_path):
    api = FakePve()
    ctx, src, tgt, e = _env(tmp_path, api)
    st = MigrationRunner(MigrationConfig(name="m", source="pve", target="tgt", vm="RIS-Sainath"), e, src, tgt, ctx,
                         StateStore(e.state_dir)).run()
    assert st.phase.value == "done" and st.morph["status"] != "manual_required"   # virtio-scsi -> bus/driver match
    assert len(tgt.vms["RIS-Sainath"]["disks"]) == 13
    for i in range(13):
        assert tgt._final(tgt.disks[f"RIS-Sainath-disk{i:02d}"]).read_bytes() == PatternDisk(f"RIS-Sainath:{i}", SIZE).read(0, SIZE)
    assert api.deleted and not api.snaps


@pytest.mark.parametrize("setup,match", [
    (lambda a: setattr(a, "storage_type", "lvm"), "has no snapshots"),
    (lambda a: setattr(a, "c", cfg(agent="0")), "guest agent"),
    (lambda a: setattr(a, "pend", [{"key": "scsi3", "pending": "x"}]), "pending"),
])
def test_refusals(tmp_path, setup, match):
    api = FakePve()
    setup(api)
    ctx, src, tgt, e = _env(tmp_path, api)
    with pytest.raises(SafetyGateError, match=match):
        MigrationRunner(MigrationConfig(name="m", source="pve", target="tgt", vm="RIS-Sainath"), e, src, tgt, ctx,
                        StateStore(e.state_dir)).run()
    assert not api.snaps and not tgt.vms


def _sync(tmp_path, api, shell, store):
    ctx, src, tgt, e = _env(tmp_path, api, shell)
    rr = ReplicaRunner(MigrationConfig(name="rep", source="pve", target="tgt", vm="RIS-Sainath"), e, src, tgt, ctx, store)
    ts = MemoryTaskStore(sync_dag(False))
    assert DagRunner(ts, rr.sync_handlers()).run().value == "completed", [t.error for t in ts.load_tasks() if t.error]
    return rr, tgt


def test_rbd_diff_replica_keeps_previous_snapshot_as_base(tmp_path):
    api, store = FakePve(), MemoryReplicaStore()
    shell = FakeShell()
    rr, _ = _sync(tmp_path, api, shell, store)
    first = rr.state.base_snapshot["id"]
    assert list(api.snaps) == [first]                                     # kept on the VM as the diff base
    api.gen = 1
    shell.rbd = {f"vm-100-disk-{i}": [{"offset": b * (64 << 10), "length": 64 << 10, "exists": "true"}
                                      for b in PatternDisk(f"RIS-Sainath:{i}", SIZE).changed_blocks(0, 1)]
                 for i in range(13)}
    rr, tgt = _sync(tmp_path, api, shell, store)
    last = rr.state.syncs[-1]
    assert last["modes"] == ["cbt"] and last["bytes_read"] < 13 * SIZE // 3
    assert any(c[:2] == ["rbd", "diff"] and "--from-snap" in c and first in c for c in shell.cmds)
    assert first in api.deleted and len(api.snaps) == 1                   # base rotated
    for i in range(13):
        assert tgt._final(tgt.disks[f"rep-r{i:02d}"]).read_bytes() == PatternDisk(f"RIS-Sainath:{i}", SIZE, generation=1).read(0, SIZE)


def test_non_rbd_replica_uses_compare_and_keeps_no_snapshot(tmp_path):
    api, store = FakePve(storage_type="lvmthin"), MemoryReplicaStore()
    _sync(tmp_path, api, FakeShell(storage="lvmthin"), store)
    api.gen = 1
    sh = FakeShell(storage="lvmthin")
    rr, tgt = _sync(tmp_path, api, sh, store)
    assert not any(c[:2] == ["rbd", "diff"] for c in sh.cmds)
    assert rr.state.syncs[-1]["modes"] == ["compare"] and not api.snaps
    assert tgt._final(tgt.disks["rep-r05"]).read_bytes() == PatternDisk("RIS-Sainath:5", SIZE, generation=1).read(0, SIZE)
