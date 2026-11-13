"""Purpose: Google Cloud source tests without Google Cloud. A fake HTTP layer plays the OAuth token
endpoint (it verifies the service-account JWT signature) and Compute Engine (aggregated instances,
disks, machine types, createSnapshot with guestFlush, zone operations + wait, temporary disks,
attachDisk/detachDisk, deletes). Attaching a disk writes its bytes to a fake /dev/disk/by-id
directory, which the real GcpSource reads as a block device.

Usage: pytest tests/test_gcp.py
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.parse

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from vmigrate.config.loader import EngineConfig, MigrationConfig
from vmigrate.core.context import RunContext
from vmigrate.core.errors import DiskCountMismatch, SafetyGateError
from vmigrate.engine import MigrationRunner, StateStore
from vmigrate.engine.dag import DagRunner, MemoryTaskStore
from vmigrate.engine.replica import MemoryReplicaStore, ReplicaRunner, sync_dag
from vmigrate.engine.safety import check_disk_counts
from vmigrate.ir import DiskBus, Firmware, OsFamily
from vmigrate.providers.fake.provider import FakeTarget, PatternDisk
from vmigrate.providers.gcp import mapping
from vmigrate.providers.gcp.api import GceApi
from vmigrate.providers.gcp.source import GcpSource

UNIT = 6 << 20
C = "https://compute.googleapis.com/compute/v1/projects/prod-1"
Z = "europe-west1-b"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SA = json.dumps({"type": "service_account", "client_email": "mig@prod-1.iam.gserviceaccount.com",
                 "private_key_id": "k1", "token_uri": "https://oauth2.googleapis.com/token",
                 "private_key": KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()).decode()})


def content(name: str, gen: int) -> bytes:
    return PatternDisk(f"gce:{name}", UNIT, generation=gen).read(0, UNIT)


def inst(status="TERMINATED", disks=("boot", "data1", "data2"), **over):
    i = {"id": "4242", "name": "erp01", "zone": f"{C}/zones/{Z}", "status": status, "selfLink": f"{C}/zones/{Z}/instances/erp01",
         "machineType": f"{C}/zones/{Z}/machineTypes/n2-standard-4",
         "shieldedInstanceConfig": {"enableSecureBoot": True},
         "disks": [{"source": f"{C}/zones/{Z}/disks/erp01-{d}", "boot": k == 0, "index": k, "deviceName": d,
                    "interface": "SCSI", "type": "PERSISTENT", "mode": "READ_WRITE"} for k, d in enumerate(disks)],
         "networkInterfaces": [{"name": "nic0", "network": f"{C}/global/networks/prod", "subnetwork":
                                f"https://x/regions/europe-west1/subnetworks/app", "networkIP": "10.10.0.5", "nicType": "GVNIC"}]}
    i.update(over)
    return i


def disk_json(d):
    feats = [{"type": "UEFI_COMPATIBLE"}, {"type": "GVNIC"}] if d == "boot" else []
    return {"name": f"erp01-{d}", "selfLink": f"{C}/zones/{Z}/disks/erp01-{d}", "zone": f"{C}/zones/{Z}",
            "sizeGb": "1", "type": f"{C}/zones/{Z}/diskTypes/pd-ssd", "users": [f"{C}/zones/{Z}/instances/erp01"],
            "guestOsFeatures": feats,
            "licenses": ["https://www.googleapis.com/compute/v1/projects/debian-cloud/global/licenses/debian-12-bookworm"]
            if d == "boot" else []}


class FakeGce:
    def __init__(self, devdir, status="TERMINATED"):
        self.devdir, self.status = devdir, status
        self.gen = 0
        self.snaps: dict[str, dict] = {}
        self.disks: dict[str, dict] = {}       # temporary disks in the worker zone
        self.attached: dict[str, str] = {}
        self.ops: dict[str, dict] = {}
        self.k = 0
        self.flush: list[bool] = []
        self.tokens = 0

    def op(self, kind, target):
        self.k += 1
        url = f"{C}/zones/{Z}/operations/op{self.k}"
        self.ops[url] = {"name": f"op{self.k}", "status": "RUNNING", "selfLink": url, "operationType": kind,
                         "targetLink": target}
        return 200, {}, json.dumps(self.ops[url]).encode()

    def ok(self, d, st=200):
        return st, {}, json.dumps(d).encode()

    def __call__(self, method, url, headers, body, timeout):
        if url == "https://oauth2.googleapis.com/token":
            assertion = urllib.parse.parse_qs(body.decode())["assertion"][0]
            h, c, s = assertion.split(".")
            pad = lambda x: base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))  # noqa: E731
            KEY.public_key().verify(pad(s), f"{h}.{c}".encode(), padding.PKCS1v15(), hashes.SHA256())
            assert json.loads(pad(c))["scope"] == "https://www.googleapis.com/auth/compute"
            self.tokens += 1
            return self.ok({"access_token": "tok", "expires_in": 3600})
        assert headers["Authorization"] == "Bearer tok"
        u = urllib.parse.urlsplit(url)
        q = urllib.parse.parse_qs(u.query)
        p = "https://" + u.netloc + u.path
        data = json.loads(body) if body else None
        if p in self.ops or p.removesuffix("/wait") in self.ops:
            o = self.ops[p.removesuffix("/wait")]
            o["status"] = "DONE"
            return self.ok(o)
        if p == f"{C}/aggregated/instances":
            if "pageToken" in q:
                return self.ok({"items": {"zones/us-east1-b": {"instances": [dict(inst(), name="erp01", zone=f"{C}/zones/us-east1-b", id="9")]}}})
            return self.ok({"items": {f"zones/{Z}": {"instances": [inst(self.status)]}, "zones/x": {"warning": {}}},
                            "nextPageToken": "p2"})
        if p == f"{C}/zones/{Z}/instances/erp01" and method == "GET":
            return self.ok(inst(self.status))
        if p == f"{C}/zones/{Z}/instances/erp01/stop":
            self.status = "TERMINATED"
            return self.op("stop", p)
        if p == f"{C}/zones/{Z}/machineTypes/n2-standard-4":
            return self.ok({"guestCpus": 4, "memoryMb": 16384})
        if p == f"{C}/zones/{Z}/disks" and method == "GET":
            return self.ok({"items": [disk_json(d) for d in ("boot", "data1", "data2")] + [{"name": "other", "users": []}]})
        if p == f"{C}/zones/{Z}/disks" and method == "POST":
            snap = self.snaps[data["sourceSnapshot"].rsplit("/", 1)[-1]]
            assert data["type"].endswith("/diskTypes/pd-balanced") and data["labels"]["vmigrate-run"]
            self.disks[data["name"]] = {"name": data["name"], "labels": data["labels"], "src": snap}
            return self.op("insert", data["name"])
        if p.startswith(f"{C}/zones/{Z}/disks/") and p.endswith("/createSnapshot"):
            disk = p.split("/")[-2].removeprefix("erp01-")
            self.flush.append(q.get("guestFlush") == ["true"])
            self.snaps[data["name"]] = {"name": data["name"], "labels": data["labels"], "status": "READY",
                                        "disk": disk, "gen": self.gen}
            return self.op("createSnapshot", data["name"])
        if p.startswith(f"{C}/zones/{Z}/disks/"):
            name = p.rsplit("/", 1)[-1]
            if method == "GET":
                if name.startswith("erp01-"):
                    return self.ok(disk_json(name.removeprefix("erp01-")))
                d = self.disks.get(name)
                return self.ok(d) if d else (404, {}, b'{"error":{"code":404,"message":"not found"}}')
            if method == "DELETE":
                assert name not in self.attached, "deleting an attached disk"
                self.disks.pop(name)
                return self.op("delete", name)
        if p.startswith(f"{C}/global/snapshots/"):
            name = p.rsplit("/", 1)[-1]
            s = self.snaps.get(name)
            if s is None:
                return 404, {}, b'{"error":{"code":404,"message":"not found"}}'
            if method == "DELETE":
                del self.snaps[name]
                return self.op("delete", name)
            return self.ok({k: v for k, v in s.items() if k in ("name", "labels", "status")})
        if p == f"{C}/zones/{Z}/instances/worker1/attachDisk":
            assert data["mode"] == "READ_ONLY"
            name = data["source"].rsplit("/", 1)[-1]
            s = self.disks[name]["src"]
            with open(os.path.join(self.devdir, f"google-{data['deviceName']}"), "wb") as f:
                f.write(content(s["disk"], s["gen"]))
            self.attached[name] = data["deviceName"]
            return self.op("attachDisk", name)
        if p == f"{C}/zones/{Z}/instances/worker1/detachDisk":
            dev = q["deviceName"][0]
            name = next(n for n, d in self.attached.items() if d == dev)
            del self.attached[name]
            os.remove(os.path.join(self.devdir, f"google-{dev}"))
            return self.op("detachDisk", name)
        raise AssertionError(f"unexpected {method} {url}")


def make(tmp_path, g, **cfg):
    ctx = RunContext(dry_run=False, logger=logging.getLogger("t"))
    devdir = g.devdir
    api = GceApi(project="prod-1", key_json=SA, http=g, poll=0, sleep=lambda s: None)
    src = GcpSource("gcp", {"type": "gcp", "project": "prod-1", **cfg}, ctx, api=api, worker=(Z, "worker1"),
                    by_id_dir=devdir, dev_timeout=2)
    tgt = FakeTarget("tgt", {"staging_dir": str(tmp_path / "tgt"), "persist": True}, ctx)
    e = EngineConfig(state_dir=tmp_path / "s", report_dir=tmp_path / "r", chunk_size=1 << 20, checkpoint_every=2 << 20,
                     verify_samples=4, verify_sample_bytes=64 << 10)
    return ctx, src, tgt, e


@pytest.fixture(autouse=True)
def small_gib(monkeypatch):
    monkeypatch.setattr(mapping, "GIB", UNIT)


def fake(tmp_path, **kw):
    d = tmp_path / "by-id"
    d.mkdir(exist_ok=True)
    return FakeGce(str(d), **kw)


def test_mapping_and_refusals():
    disks = {f"{C}/zones/{Z}/disks/erp01-{d}": disk_json(d) for d in ("boot", "data1", "data2")}
    mt = {"guestCpus": 4, "memoryMb": 16384}
    spec = mapping.to_vmspec(inst(), disks, mt)
    assert [d.label for d in spec.ordered_disks()] == ["boot", "data1", "data2"] and spec.boot_disk().label == "boot"
    assert spec.disks[0].controller == "virtio-scsi" and spec.disks[0].bus == DiskBus.SCSI
    assert spec.firmware == Firmware.UEFI and spec.secure_boot and spec.cpu.total == 4 and spec.memory_bytes == 16 << 30
    assert spec.guest_os.family == OsFamily.LINUX and spec.guest_os.distro == "debian"
    assert spec.nics[0].network == "prod/app" and spec.nics[0].model == "gvnic"
    assert check_disk_counts(spec, mapping.crosscheck(inst(), list(disks.values())))
    with pytest.raises(DiskCountMismatch):
        check_disk_counts(spec, mapping.crosscheck(inst(), list(disks.values())[:1]))
    ssd = inst()
    ssd["disks"].append({"type": "SCRATCH", "interface": "NVME", "index": 9})
    with pytest.raises(mapping.GcpMappingError, match="local SSD"):
        mapping.to_vmspec(ssd, disks, mt)
    assert mapping.to_vmspec(ssd, disks, mt, ignore_local_ssd=True)
    with pytest.raises(mapping.GcpMappingError, match="not returned"):
        mapping.to_vmspec(inst(), dict(list(disks.items())[:2]), mt)


def test_find_vm_and_jwt_signin(tmp_path):
    g = fake(tmp_path)
    _, src, _, _ = make(tmp_path, g)
    assert sorted(v.id for v in src.list_vms()) == [f"{Z}/erp01", "us-east1-b/erp01"]
    with pytest.raises(SafetyGateError, match="zone"):
        src.find_vm("erp01")
    assert src.find_vm(f"{Z}/erp01").name == "erp01" and src.find_vm("9").id == "us-east1-b/erp01"
    assert src.check_connection() == [] and g.tokens == 1           # signed JWT accepted once, token cached


def test_migration_attaches_temp_disks_and_cleans_up(tmp_path):
    g = fake(tmp_path)
    ctx, src, tgt, e = make(tmp_path, g)
    job = MigrationConfig(name="m", source="gcp", target="tgt", vm=f"{Z}/erp01", app_consistent=True,
                          osmorph={"allow_manual": True})
    st = MigrationRunner(job, e, src, tgt, ctx, StateStore(e.state_dir)).run()
    assert st.phase.value == "done" and g.flush == [True, True, True]
    for k, d in enumerate(("boot", "data1", "data2")):
        assert tgt._final(tgt.disks[f"erp01-disk{k:02d}"]).read_bytes() == content(d, 0)
    assert not g.snaps and not g.disks and not g.attached and not os.listdir(g.devdir)


def test_running_multi_disk_needs_opt_in(tmp_path):
    g = fake(tmp_path, status="RUNNING")
    ctx, src, tgt, e = make(tmp_path, g)
    job = MigrationConfig(name="m", source="gcp", target="tgt", vm=f"{Z}/erp01", app_consistent=False,
                          osmorph={"allow_manual": True})
    with pytest.raises(SafetyGateError, match="allow_per_disk_snapshots"):
        MigrationRunner(job, e, src, tgt, ctx, StateStore(e.state_dir)).run()
    assert not g.snaps
    ctx, src, tgt, e = make(tmp_path / "b", g, allow_per_disk_snapshots=True)
    assert MigrationRunner(job, e, src, tgt, ctx, StateStore(e.state_dir)).run().phase.value == "done"


def test_replica_uses_compare_and_cutover(tmp_path):
    g, store = fake(tmp_path), MemoryReplicaStore()

    def sync():
        ctx, src, tgt, e = make(tmp_path, g)
        rr = ReplicaRunner(MigrationConfig(name="rep", source="gcp", target="tgt", vm=f"{Z}/erp01", app_consistent=False),
                           e, src, tgt, ctx, store)
        ts = MemoryTaskStore(sync_dag(False))
        assert DagRunner(ts, rr.sync_handlers()).run().value == "completed", [t.error for t in ts.load_tasks() if t.error]
        return rr, tgt, src

    sync()
    g.gen = 1
    rr, tgt, src = sync()
    assert "cbt" not in rr.state.syncs[-1]["modes"] and not g.snaps and not g.disks
    for k, d in enumerate(("boot", "data1", "data2")):
        assert tgt._final(tgt.disks[f"rep-r{k:02d}"]).read_bytes() == content(d, 1)
    g.status = "RUNNING"
    vm = src.find_vm(f"{Z}/erp01")
    assert src.shutdown_vm(vm) == "erp01 stopped" and src.shutdown_vm(vm) == "erp01 already stopped"
