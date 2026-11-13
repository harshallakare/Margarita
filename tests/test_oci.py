"""Purpose: Oracle Cloud source tests without OCI. A fake HTTP layer plays the Core Services API and
verifies every request's HTTP signature (rsa-sha256, signed headers, body digest): instances,
boot/block volume attachments, volumes, images, VNICs, volume backups, restores, consistent device
paths, attach/detach and deletes. Attaching writes the volume's bytes to a fake device file.

Usage: pytest tests/test_oci.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
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
from vmigrate.ir import Firmware, OsFamily
from vmigrate.providers.fake.provider import FakeTarget, PatternDisk
from vmigrate.providers.oci import mapping
from vmigrate.providers.oci.api import OciApi
from vmigrate.providers.oci.source import OciSource

UNIT = 6 << 20
BASE = "https://iaas.eu-frankfurt-1.oraclecloud.com/20160918"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
COMP = "ocid1.compartment.oc1..c"
IID = "ocid1.instance.oc1..app"
AD = "Uocm:EU-FRANKFURT-1-AD-1"
VOLS = ["ocid1.bootvolume.oc1..b0", "ocid1.volume.oc1..v1", "ocid1.volume.oc1..v2"]


def content(vid: str, gen: int) -> bytes:
    return PatternDisk(f"oci:{vid}", UNIT, generation=gen).read(0, UNIT)


INSTANCE = {"id": IID, "displayName": "app01", "availabilityDomain": AD, "compartmentId": COMP, "shape": "VM.Standard.E4.Flex",
            "shapeConfig": {"ocpus": 2, "vcpus": 4, "memoryInGBs": 32}, "launchMode": "PARAVIRTUALIZED",
            "launchOptions": {"bootVolumeType": "PARAVIRTUALIZED", "firmware": "UEFI_64", "networkType": "PARAVIRTUALIZED"},
            "platformConfig": {"isSecureBootEnabled": True}, "sourceDetails": {"imageId": "ocid1.image.oc1..ol8"}}


class FakeOci:
    def __init__(self, devdir, state="STOPPED"):
        self.devdir, self.state, self.gen = devdir, state, 0
        self.backups: dict[str, dict] = {}
        self.restored: dict[str, dict] = {}
        self.attachments: dict[str, dict] = {}
        self.k = 0
        self.signed = 0

    def verify(self, method, url, headers, body):
        m = re.match(r'Signature version="1",keyId="([^"]+)",algorithm="rsa-sha256",headers="([^"]+)",signature="([^"]+)"',
                     headers["authorization"])
        assert m and m.group(1) == "ocid1.tenancy.oc1..t/ocid1.user.oc1..u/aa:bb"
        u = urllib.parse.urlsplit(url)
        names = m.group(2).split()
        if method in ("POST", "PUT"):
            assert headers["x-content-sha256"] == base64.b64encode(hashlib.sha256(body or b"").digest()).decode()
            assert {"x-content-sha256", "content-type", "content-length"} <= set(names)
        target = f"{method.lower()} {u.path}" + (f"?{u.query}" if u.query else "")
        signing = "\n".join(f"{n}: {target if n == '(request-target)' else headers[n]}" for n in names)
        KEY.public_key().verify(base64.b64decode(m.group(3)), signing.encode(), padding.PKCS1v15(), hashes.SHA256())
        assert headers["host"] == u.netloc
        self.signed += 1

    def ok(self, d, hdr=None):
        return 200, hdr or {}, json.dumps(d).encode()

    def __call__(self, method, url, headers, body, timeout):
        self.verify(method, url, headers, body)
        u = urllib.parse.urlsplit(url)
        p = u.path.removeprefix("/20160918")
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        data = json.loads(body) if body else None
        if p == "/instances" and method == "GET":
            if q.get("page") == "2":
                return self.ok([{"id": "ocid1.instance.oc1..old", "displayName": "gone", "lifecycleState": "TERMINATED"}])
            return self.ok([dict(INSTANCE, lifecycleState=self.state)], {"opc-next-page": "2"})
        if p == f"/instances/{IID}" and method == "GET":
            return self.ok(dict(INSTANCE, lifecycleState=self.state))
        if p == f"/instances/{IID}" and method == "POST":
            assert q["action"] in ("SOFTSTOP", "STOP")
            self.state = "STOPPED"
            return self.ok(dict(INSTANCE, lifecycleState="STOPPING"))
        if p == "/instances/ocid1.instance.oc1..worker/devices":
            used = {a["device"] for a in self.attachments.values()}
            return self.ok([{"name": f"{self.devdir}/oraclevd{c}", "isAvailable": True} for c in "bcdefg"
                            if f"{self.devdir}/oraclevd{c}" not in used])
        if p == "/bootVolumeAttachments":
            assert q["instanceId"] == IID and q["availabilityDomain"] == AD
            return self.ok([{"bootVolumeId": VOLS[0], "lifecycleState": "ATTACHED"}])
        if p == "/volumeAttachments" and method == "GET":
            mine = [{"volumeId": v, "instanceId": IID, "lifecycleState": "ATTACHED", "attachmentType": "iscsi" if k == 2 else "paravirtualized",
                     "device": f"/dev/oracleoci/oraclevd{'bc'[k - 1]}", "timeCreated": f"2026-0{k}"} for k, v in enumerate(VOLS) if k]
            other = [{"volumeId": "ocid1.volume.oc1..x", "instanceId": "ocid1.instance.oc1..other", "lifecycleState": "ATTACHED"}]
            return self.ok(mine if q.get("instanceId") else mine + other)
        if p.startswith(("/bootVolumes/", "/volumes/")) and method == "GET":
            vid = p.rsplit("/", 1)[-1]
            if vid in self.restored:
                r = self.restored[vid]
                return self.ok({"id": vid, "lifecycleState": r["state"], "freeformTags": r["tags"]})
            if vid not in VOLS:
                return 404, {}, b'{"code":"NotAuthorizedOrNotFound","message":"x"}'
            return self.ok({"id": vid, "sizeInGBs": 1, "sizeInMBs": UNIT >> 20, "displayName": vid[-2:], "lifecycleState": "AVAILABLE"})
        if p == "/images/ocid1.image.oc1..ol8":
            return self.ok({"operatingSystem": "Oracle Linux", "operatingSystemVersion": "8"})
        if p == "/vnicAttachments":
            return self.ok([{"vnicId": "ocid1.vnic.oc1..n1", "lifecycleState": "ATTACHED"}])
        if p == "/vnics/ocid1.vnic.oc1..n1":
            return self.ok({"id": "ocid1.vnic.oc1..n1", "isPrimary": True, "macAddress": "02:00:17:AA:BB:CC",
                            "privateIp": "10.0.1.7", "subnetId": "ocid1.subnet.oc1.eu-frankfurt-1.app"})
        if p in ("/bootVolumeBackups", "/volumeBackups") and method == "POST":
            vid = data.get("bootVolumeId") or data.get("volumeId")
            assert data["type"] == "FULL" and data["freeformTags"]["vmigrate-run"].startswith("vmigrate-")
            self.k += 1
            bid = f"ocid1.backup.oc1..{self.k}"
            self.backups[bid] = {"vol": vid, "gen": self.gen, "tags": data["freeformTags"], "polls": 0}
            return self.ok({"id": bid, "lifecycleState": "CREATING"})
        if p.startswith(("/bootVolumeBackups/", "/volumeBackups/")):
            bid = p.rsplit("/", 1)[-1]
            b = self.backups.get(bid)
            if b is None:
                return 404, {}, b'{"code":"NotAuthorizedOrNotFound","message":"x"}'
            if method == "DELETE":
                del self.backups[bid]
                return self.ok({})
            b["polls"] += 1
            return self.ok({"id": bid, "lifecycleState": "AVAILABLE" if b["polls"] > 1 else "CREATING", "freeformTags": b["tags"]})
        if p in ("/bootVolumes", "/volumes") and method == "POST":
            assert data["availabilityDomain"] == AD
            b = self.backups[data["sourceDetails"]["id"]]
            self.k += 1
            vid = f"ocid1.volume.oc1..r{self.k}"
            self.restored[vid] = {"src": b, "tags": data["freeformTags"], "state": "AVAILABLE"}
            return self.ok({"id": vid, "lifecycleState": "PROVISIONING"})
        if p.startswith(("/bootVolumes/", "/volumes/")) and method == "DELETE":
            vid = p.rsplit("/", 1)[-1]
            assert not any(a["vol"] == vid for a in self.attachments.values()), "deleting an attached volume"
            del self.restored[vid]
            return self.ok({})
        if p == "/volumeAttachments" and method == "POST":
            assert data["isReadOnly"] is True and data["type"] == "paravirtualized"
            r = self.restored[data["volumeId"]]
            with open(data["device"], "wb") as f:
                f.write(content(r["src"]["vol"], r["src"]["gen"]))
            self.k += 1
            aid = f"ocid1.volumeattachment.oc1..{self.k}"
            self.attachments[aid] = {"vol": data["volumeId"], "device": data["device"]}
            return self.ok({"id": aid, "lifecycleState": "ATTACHING"})
        if p.startswith("/volumeAttachments/"):
            aid = p.rsplit("/", 1)[-1]
            if method == "DELETE":
                os.remove(self.attachments.pop(aid)["device"])
                return self.ok({})
            return self.ok({"id": aid, "lifecycleState": "ATTACHED"}) if aid in self.attachments else (404, {}, b"{}")
        raise AssertionError(f"unexpected {method} {url}")


def fake(tmp_path, **kw):
    d = tmp_path / "dev"
    d.mkdir(exist_ok=True)
    return FakeOci(str(d), **kw)


def make(tmp_path, f, **cfg):
    ctx = RunContext(dry_run=False, logger=logging.getLogger("t"))
    api = OciApi(region="eu-frankfurt-1", tenancy="ocid1.tenancy.oc1..t", user="ocid1.user.oc1..u", fingerprint="aa:bb",
                 key_pem=PEM, http=f, poll=0, sleep=lambda s: None)
    src = OciSource("oci", {"type": "oci", "region": "eu-frankfurt-1", "compartment_ocid": COMP, **cfg}, ctx, api=api,
                    worker={"id": "ocid1.instance.oc1..worker", "availabilityDomain": AD, "compartmentId": COMP},
                    dev_timeout=2)
    tgt = FakeTarget("tgt", {"staging_dir": str(tmp_path / "tgt"), "persist": True}, ctx)
    e = EngineConfig(state_dir=tmp_path / "s", report_dir=tmp_path / "r", chunk_size=1 << 20, checkpoint_every=2 << 20,
                     verify_samples=4, verify_sample_bytes=64 << 10)
    return ctx, src, tgt, e


def test_mapping_signing_and_gate(tmp_path):
    f = fake(tmp_path)
    _, src, _, _ = make(tmp_path, f)
    assert [v.name for v in src.list_vms()] == ["app01"]                 # paged; terminated hidden
    vm = src.find_vm("app01")
    spec = src.inspect_vm(vm)
    assert [d.id for d in spec.ordered_disks()] == VOLS and spec.boot_disk().label == "boot"
    assert spec.disks[0].controller == "oci-paravirtualized" and spec.disks[2].controller == "oci-iscsi"
    assert spec.extra["oci"]["iscsi_volumes"] == [VOLS[2]]
    assert spec.firmware == Firmware.UEFI and spec.secure_boot and spec.cpu.total == 4 and spec.memory_bytes == 32 << 30
    assert spec.guest_os.family == OsFamily.LINUX and spec.guest_os.distro == "ol"
    assert spec.nics[0].mac == "02:00:17:aa:bb:cc" and spec.nics[0].ipv4 == "10.0.1.7"
    assert check_disk_counts(spec, src.disk_count_crosscheck(vm)) and f.signed > 5
    with pytest.raises(DiskCountMismatch):
        check_disk_counts(spec, mapping.crosscheck([{}], [{}], [], IID))


def test_migration_restores_attaches_and_cleans_up(tmp_path):
    f = fake(tmp_path)
    ctx, src, tgt, e = make(tmp_path, f)
    job = MigrationConfig(name="m", source="oci", target="tgt", vm="app01", app_consistent=False,
                          osmorph={"allow_manual": True})
    st = MigrationRunner(job, e, src, tgt, ctx, StateStore(e.state_dir)).run()
    assert st.phase.value == "done"
    for k, v in enumerate(VOLS):
        assert tgt._final(tgt.disks[f"app01-disk{k:02d}"]).read_bytes() == content(v, 0)
    assert not f.backups and not f.restored and not f.attachments and not os.listdir(f.devdir)


def test_refusals(tmp_path):
    f = fake(tmp_path, state="RUNNING")
    ctx, src, tgt, e = make(tmp_path, f)
    vm = src.find_vm("app01")
    src.inspect_vm(vm)
    with pytest.raises(SafetyGateError, match="allow_per_disk_snapshots"):
        src.create_snapshot(vm, False)
    with pytest.raises(SafetyGateError, match="crash-consistent"):
        src.create_snapshot(vm, True)
    assert not f.backups


def test_replica_compare_and_cutover(tmp_path):
    f, store = fake(tmp_path), MemoryReplicaStore()

    def sync():
        ctx, src, tgt, e = make(tmp_path, f)
        rr = ReplicaRunner(MigrationConfig(name="rep", source="oci", target="tgt", vm="app01", app_consistent=False),
                           e, src, tgt, ctx, store)
        ts = MemoryTaskStore(sync_dag(False))
        assert DagRunner(ts, rr.sync_handlers()).run().value == "completed", [t.error for t in ts.load_tasks() if t.error]
        return rr, tgt, src

    sync()
    f.gen = 1
    rr, tgt, src = sync()
    assert "cbt" not in rr.state.syncs[-1]["modes"] and not f.backups and not f.restored
    for k, v in enumerate(VOLS):
        assert tgt._final(tgt.disks[f"rep-r{k:02d}"]).read_bytes() == content(v, 1)
    f.state = "RUNNING"
    vm = src.find_vm("app01")
    assert src.shutdown_vm(vm) == "app01 stopped" and src.shutdown_vm(vm) == "app01 already stopped"
