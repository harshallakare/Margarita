"""Purpose: oVirt / RHV / OLVM source tests without an engine. A fake HTTP layer plays the engine
REST API v4 (VMs, disk attachments, disks, NICs, vNIC profiles, backups, checkpoints, image
transfers) and the hosts' ImageIO (range reads, extents?context=zero|dirty). The real OvirtApi,
ImageioReader and OvirtSource run on top of it, through the migration and replica engines.

Usage: pytest tests/test_ovirt.py
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.parse

import pytest

from vmigrate.config.loader import EngineConfig, MigrationConfig
from vmigrate.core.context import RunContext
from vmigrate.core.errors import DiskCountMismatch, ProviderError
from vmigrate.engine import MigrationRunner, StateStore
from vmigrate.engine.dag import DagRunner, MemoryTaskStore
from vmigrate.engine.replica import MemoryReplicaStore, ReplicaRunner, sync_dag
from vmigrate.engine.safety import check_disk_counts
from vmigrate.ir import DiskBus, Firmware, OsFamily
from vmigrate.providers.fake.provider import FakeTarget, PatternDisk
from vmigrate.providers.ovirt import mapping
from vmigrate.providers.ovirt.api import OvirtApi
from vmigrate.providers.ovirt.source import OvirtSource

UNIT = 6 << 20
GRAIN = 64 << 10
API = "https://engine.example.com/ovirt-engine/api"
DISKS = [("d0", "db01_os", "virtio_scsi", "true", "incremental"), ("d1", "db01_data", "virtio", "false", "incremental"),
         ("d2", "db01_logs", "sata", "false", "none")]


def content(did: str, gen: int) -> bytes:
    return PatternDisk(f"db01:{did}", UNIT, generation=gen).read(0, UNIT)


VM = {"id": "vm-1", "name": "db01", "status": "up", "memory": str(8 << 30),
      "cpu": {"topology": {"sockets": "2", "cores": "2", "threads": "1"}},
      "bios": {"type": "q35_secure_boot"}, "os": {"type": "rhel_9x64"}}


def attachments() -> list:
    return [{"id": d, "disk": {"id": d}, "interface": i, "bootable": b, "active": "true", "logical_name": f"/dev/x{d}"}
            for d, _, i, b, _ in reversed(DISKS)]          # boot disk not first on purpose


def disk_json(did: str) -> dict:
    d = next(x for x in DISKS if x[0] == did)
    return {"id": did, "alias": d[1], "provisioned_size": str(UNIT), "format": "cow", "backup": d[4],
            "storage_type": "image", "shareable": "false", "status": "ok"}


NICS = [{"id": "n1", "name": "nic1", "interface": "virtio", "mac": {"address": "56:6f:aa:00:00:01"},
         "vnic_profile": {"id": "p1"}}]


class FakeEngine:
    def __init__(self):
        self.gen = 0
        self.backups: dict[str, dict] = {}
        self.checkpoints: list[dict] = []          # chain, oldest first
        self.cp_gen: dict[str, int] = {}
        self.transfers: dict[str, dict] = {}       # ticket -> {backup, disk}
        self.k = 0
        self.calls: list[tuple[str, str]] = []
        self.blob_errors = 0
        self.status = "up"

    def ok(self, d, st=200):
        return st, {}, json.dumps(d).encode()

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url))
        u = urllib.parse.urlsplit(url)
        if u.netloc == "host1:54322":
            return self.imageio(u, headers)
        assert headers["Authorization"] == "Basic " + base64.b64encode(b"admin@internal:pw").decode()
        path = u.path.removeprefix("/ovirt-engine/api")
        data = json.loads(body) if body else None
        return self.engine(method, path, urllib.parse.parse_qs(u.query), data)

    def engine(self, m, p, q, data):
        if m == "GET" and p == "":
            return self.ok({"product_info": {"version": {"full_version": "4.5.6", "major": "4", "minor": "5"}}})
        if m == "GET" and p == "/vms":
            return self.ok({"vm": [dict(VM, status=self.status), {"id": "vm-2", "name": "web01", "status": "down"}]})
        if m == "GET" and p == "/vms/vm-1":
            return self.ok(dict(VM, status=self.status))
        if m == "GET" and p == "/vms/vm-1/diskattachments":
            return self.ok({"disk_attachment": attachments()})
        if m == "GET" and p.startswith("/disks/"):
            return self.ok(disk_json(p.rsplit("/", 1)[-1]))
        if m == "GET" and p == "/disks":
            assert q["search"] == ["vm_names=db01"]
            return self.ok({"disk": [disk_json(d[0]) for d in DISKS]})
        if m == "GET" and p == "/vms/vm-1/nics":
            return self.ok({"nic": NICS})
        if m == "GET" and p == "/vnicprofiles/p1":
            return self.ok({"id": "p1", "name": "ovirtmgmt", "network": {"id": "net1"}})
        if m == "GET" and p == "/networks/net1":
            return self.ok({"id": "net1", "name": "prod-vlan20"})
        if m == "POST" and p == "/vms/vm-1/shutdown":
            self.status = "down"
            return self.ok({})
        if p == "/vms/vm-1/checkpoints":
            return self.ok({"checkpoint": self.checkpoints})
        if m == "DELETE" and p.startswith("/vms/vm-1/checkpoints/"):
            cid = p.rsplit("/", 1)[-1]
            if self.checkpoints and self.checkpoints[0]["id"] != cid:
                return 409, {}, b'{"reason":"Operation Failed","detail":"only the root checkpoint can be removed"}'
            self.checkpoints = [c for c in self.checkpoints if c["id"] != cid]
            if self.checkpoints:
                self.checkpoints[0]["parent_id"] = None
            return self.ok({})
        if m == "POST" and p == "/vms/vm-1/backups":
            frm = data.get("from_checkpoint_id")
            if frm and not any(c["id"] == frm for c in self.checkpoints):
                return 400, {}, b'{"reason":"Operation Failed","detail":"checkpoint does not exist"}'
            assert sorted(d["id"] for d in data["disks"]["disk"]) == ["d0", "d1", "d2"]
            self.k += 1
            bid = f"b{self.k}"
            self.backups[bid] = {"id": bid, "phase": "initializing", "gen": self.gen, "from": frm, "polls": 0,
                                 "consistency": q.get("require_consistency") == ["true"]}
            return self.ok({"id": bid, "phase": "initializing"}, 202)
        if p.startswith("/vms/vm-1/backups/"):
            bid = p.split("/")[4]
            b = self.backups.get(bid)
            if b is None:
                return 404, {}, b'{"reason":"Not Found"}'
            if m == "POST" and p.endswith("/finalize"):
                del self.backups[bid]
                for t in [t for t, x in self.transfers.items() if x["backup"] == bid]:
                    del self.transfers[t]
                return self.ok({})
            b["polls"] += 1
            if b["polls"] > 1 and b["phase"] == "initializing":
                b["phase"] = "ready"
                cid = f"cp{self.k}"
                self.checkpoints.append({"id": cid, "parent_id": self.checkpoints[-1]["id"] if self.checkpoints else None,
                                         "creation_date": f"2026-11-13T10:{self.k:02d}"})
                self.cp_gen[cid] = b["gen"]
                b["to_checkpoint_id"] = cid
            return self.ok({k: v for k, v in b.items() if k in ("id", "phase", "to_checkpoint_id")})
        if m == "POST" and p == "/imagetransfers":
            assert data["direction"] == "download" and data["format"] == "raw"
            b = self.backups[data["backup"]["id"]]
            assert b["phase"] == "ready"
            self.k += 1
            t = f"t{self.k}"
            self.transfers[t] = {"backup": data["backup"]["id"], "disk": data["disk"]["id"], "polls": 0}
            return self.ok({"id": t, "phase": "initializing"}, 201)
        if m == "GET" and p.startswith("/imagetransfers/"):
            t = p.rsplit("/", 1)[-1]
            return self.ok({"id": t, "phase": "transferring", "transfer_url": f"https://host1:54322/images/{t}",
                            "proxy_url": f"https://engine.example.com:54323/images/{t}"})
        if m == "POST" and p.startswith("/imagetransfers/") and p.endswith("/finalize"):
            self.transfers.pop(p.split("/")[2], None)
            return self.ok({})
        raise AssertionError(f"unexpected {m} {p}")

    def imageio(self, u, headers):
        parts = u.path.split("/")
        tr = self.transfers.get(parts[2])
        if tr is None:
            return 404, {}, b"no such ticket"
        b = self.backups[tr["backup"]]
        data = content(tr["disk"], b["gen"])
        if len(parts) > 3 and parts[3] == "extents":
            ctx = urllib.parse.parse_qs(u.query)["context"][0]
            if ctx == "dirty":
                if not b["from"] or disk_json(tr["disk"])["backup"] != "incremental":
                    return 404, {}, b"dirty extents are not available"
                old = content(tr["disk"], self.cp_gen[b["from"]])
                rows = [{"start": i, "length": GRAIN, "dirty": old[i:i + GRAIN] != data[i:i + GRAIN], "zero": False}
                        for i in range(0, UNIT, GRAIN)]
            else:
                rows = [{"start": i, "length": GRAIN, "zero": not any(data[i:i + GRAIN]), "hole": False}
                        for i in range(0, UNIT, GRAIN)]
            return 200, {}, json.dumps(rows).encode()
        if self.blob_errors:
            self.blob_errors -= 1
            return 503, {}, b"busy"
        a, z = (int(x) for x in headers["Range"].removeprefix("bytes=").split("-"))
        return 206, {}, data[a:z + 1]


def make(tmp_path, eng, **cfg):
    ctx = RunContext(dry_run=False, logger=logging.getLogger("t"))
    api = OvirtApi("https://engine.example.com", "admin@internal", "pw", http=eng, poll=0, sleep=lambda s: None)
    src = OvirtSource("ov", {"type": "ovirt", "url": "https://engine.example.com", "username": "admin@internal", **cfg},
                      ctx, api=api)
    tgt = FakeTarget("tgt", {"staging_dir": str(tmp_path / "tgt"), "persist": True}, ctx)
    e = EngineConfig(state_dir=tmp_path / "s", report_dir=tmp_path / "r", chunk_size=1 << 20, checkpoint_every=2 << 20,
                     verify_samples=4, verify_sample_bytes=64 << 10)
    return ctx, src, tgt, e


def test_mapping_and_refusals():
    disks = {d[0]: disk_json(d[0]) for d in DISKS}
    spec = mapping.to_vmspec(VM, attachments(), disks, NICS, {"p1": "prod-vlan20"})
    assert [d.id for d in spec.ordered_disks()] == ["d0", "d2", "d1"] and spec.boot_disk().id == "d0"
    assert spec.disks[0].controller == "virtio-scsi" and spec.disks[2].bus == DiskBus.VIRTIO
    assert spec.firmware == Firmware.UEFI and spec.secure_boot and spec.cpu.total == 4 and spec.memory_bytes == 8 << 30
    assert spec.guest_os.family == OsFamily.LINUX and spec.guest_os.distro == "rhel"
    assert spec.nics[0].network == "prod-vlan20" and spec.nics[0].mac == "56:6f:aa:00:00:01"
    assert check_disk_counts(spec, mapping.crosscheck(attachments(), list(disks.values())))
    with pytest.raises(DiskCountMismatch):
        check_disk_counts(spec, mapping.crosscheck(attachments(), list(disks.values())[:2]))
    lun = {**disks, "d1": {**disks["d1"], "storage_type": "lun"}}
    with pytest.raises(mapping.OvirtMappingError, match="direct LUN"):
        mapping.to_vmspec(VM, attachments(), lun, NICS, {})
    inactive = [dict(a, active="false") if a["id"] == "d2" else a for a in attachments()]
    with pytest.raises(mapping.OvirtMappingError, match="deactivated"):
        mapping.to_vmspec(VM, inactive, disks, NICS, {})
    assert mapping.to_vmspec(dict(VM, bios={"type": "i440fx_sea_bios"}, os={"type": "windows_2019x64"}),
                             attachments(), disks, NICS, {}).guest_os.family == OsFamily.WINDOWS
    cps = [{"id": "a", "parent_id": None, "creation_date": "1"}, {"id": "b", "parent_id": "a", "creation_date": "2"}]
    assert mapping.leaf_checkpoint(cps) == "b" and mapping.root_checkpoint(cps) == "a" and mapping.leaf_checkpoint([]) is None


def test_api_errors_and_find(tmp_path):
    eng = FakeEngine()
    _, src, _, _ = make(tmp_path, eng)
    assert src.check_connection() == [] and src.find_vm("db01").id == "vm-1"
    with pytest.raises(ProviderError):
        src.find_vm("nope")
    bad = OvirtApi("https://engine.example.com", "admin@internal", "wrong", http=eng, poll=0, sleep=lambda s: None)
    with pytest.raises(AssertionError):
        bad.vms()                 # the fake checks the credentials it was given


def test_migration_through_backup_and_imageio(tmp_path):
    eng = FakeEngine()
    ctx, src, tgt, e = make(tmp_path, eng)
    eng.blob_errors = 2
    job = MigrationConfig(name="m", source="ov", target="tgt", vm="db01", app_consistent=True,
                          osmorph={"allow_manual": True})
    st = MigrationRunner(job, e, src, tgt, ctx, StateStore(e.state_dir)).run()
    assert st.phase.value == "done"
    for i, did in enumerate(["d0", "d2", "d1"]):
        assert tgt._final(tgt.disks[f"db01-disk{i:02d}"]).read_bytes() == content(did, 0)
    assert not eng.backups and not eng.transfers and not eng.checkpoints    # finalized; its checkpoint removed
    assert all(d["copy_stats"]["bytes_read"] < UNIT for d in (x.__dict__ for x in st.disks.values()))   # zero extents skipped


def test_require_consistency_is_sent(tmp_path):
    eng = FakeEngine()
    _, src, _, _ = make(tmp_path, eng)
    vm = src.find_vm("db01")
    src.inspect_vm(vm)
    snap = src.create_snapshot(vm, True)
    assert eng.backups[snap.data["backup_id"]]["consistency"] is True
    src.cleanup(snap)
    assert not eng.backups


def test_incremental_replica_and_foreign_checkpoint(tmp_path):
    eng, store = FakeEngine(), MemoryReplicaStore()

    def sync():
        ctx, src, tgt, e = make(tmp_path, eng)
        rr = ReplicaRunner(MigrationConfig(name="rep", source="ov", target="tgt", vm="db01", app_consistent=False),
                           e, src, tgt, ctx, store)
        ts = MemoryTaskStore(sync_dag(False))
        assert DagRunner(ts, rr.sync_handlers()).run().value == "completed", [t.error for t in ts.load_tasks() if t.error]
        return rr, tgt

    sync()
    assert not eng.backups and len(eng.checkpoints) == 1
    first = eng.checkpoints[0]["id"]
    eng.gen = 1
    rr, tgt = sync()
    last = rr.state.syncs[-1]
    assert last["modes"] == ["cbt", "compare"]          # d2 has incremental backup disabled -> compare
    assert len(eng.checkpoints) == 1 and eng.checkpoints[0]["id"] != first          # old base removed
    for i, did in enumerate(["d0", "d2", "d1"]):
        assert tgt._final(tgt.disks[f"rep-r{i:02d}"]).read_bytes() == content(did, 1)
    # another backup tool took a checkpoint after ours: the next backup is incremental from IT, so no cbt
    eng.checkpoints.append({"id": "veeam-1", "parent_id": eng.checkpoints[-1]["id"], "creation_date": "2026-11-13T11:00"})
    eng.cp_gen["veeam-1"] = 1
    eng.gen = 2
    rr, tgt = sync()
    assert rr.state.syncs[-1]["modes"] == ["compare"]
    for i, did in enumerate(["d0", "d2", "d1"]):
        assert tgt._final(tgt.disks[f"rep-r{i:02d}"]).read_bytes() == content(did, 2)


def test_cutover_shutdown(tmp_path):
    eng = FakeEngine()
    _, src, _, _ = make(tmp_path, eng)
    vm = src.find_vm("db01")
    assert src.shutdown_vm(vm) == "db01 shutdown requested" and eng.status == "down"
    assert src.shutdown_vm(vm) == "db01 already down"
