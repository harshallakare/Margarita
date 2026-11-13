"""Purpose: Azure source tests without Azure. A fake HTTP layer plays Entra ID sign-in, Azure
Resource Manager (VMs, disks, NICs, sizes, incremental snapshots, beginGetAccess/endGetAccess,
long-running operations, paging, throttling) and the snapshot page blob behind the SAS (HEAD,
range GET with Content-MD5, Get Page Ranges and its diff). The real AzureClient, blob reader and
AzureSource run on top of it, through the migration and replica engines.

Usage: pytest tests/test_azure.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import urllib.parse

import pytest

from vmigrate.config.loader import EngineConfig, MigrationConfig
from vmigrate.core.context import RunContext
from vmigrate.core.errors import DiskCountMismatch, SafetyGateError
from vmigrate.engine import MigrationRunner, StateStore
from vmigrate.engine.dag import DagRunner, MemoryTaskStore
from vmigrate.engine.replica import MemoryReplicaStore, ReplicaRunner, sync_dag
from vmigrate.engine.safety import check_disk_counts
from vmigrate.ir import DiskBus, Firmware, OsFamily
from vmigrate.providers import build_source
from vmigrate.providers.azure import mapping
from vmigrate.providers.azure.blob import SasSnapshotReader, page_ranges
from vmigrate.providers.azure.client import AzureClient
from vmigrate.providers.azure.source import AzureSource
from vmigrate.providers.fake.provider import FakeTarget, PatternDisk

UNIT = 6 << 20                  # disk size in tests; PatternDisk leaves a never-written 4-6 MiB stripe
PAGE = 64 << 10                 # granularity the fake reports page ranges in
ARM = "https://management.azure.com"
SUB = "/subscriptions/sub-1"
RG = f"{SUB}/resourceGroups/rg-prod"
VM_ID = f"{RG}/providers/Microsoft.Compute/virtualMachines/RIS-Sainath"
N = 13


def disk_id(k: int) -> str:
    return f"{RG}/providers/Microsoft.Compute/disks/RIS-Sainath-{'os' if k == 0 else f'data{k}'}"


def vm_json(n: int = N, power: str = "deallocated", **over) -> dict:
    v = {"id": VM_ID, "name": "RIS-Sainath", "location": "westeurope",
         "properties": {
             "hardwareProfile": {"vmSize": "Standard_D4s_v5"},
             "storageProfile": {
                 "imageReference": {"publisher": "RedHat", "offer": "RHEL", "sku": "9-lvm-gen2"},
                 "osDisk": {"osType": "Linux", "name": "RIS-Sainath-os", "managedDisk": {"id": disk_id(0)}},
                 "dataDisks": [{"lun": k - 1, "name": f"RIS-Sainath-data{k}", "managedDisk": {"id": disk_id(k)}}
                               for k in range(n - 1, 0, -1)]},          # unsorted on purpose
             "securityProfile": {"securityType": "TrustedLaunch", "uefiSettings": {"secureBootEnabled": True}},
             "networkProfile": {"networkInterfaces": [
                 {"id": f"{RG}/providers/Microsoft.Network/networkInterfaces/nic-b"},
                 {"id": f"{RG}/providers/Microsoft.Network/networkInterfaces/nic-a"}]},
             "instanceView": {"osName": "redhat", "osVersion": "9.4",
                              "statuses": [{"code": "ProvisioningState/succeeded"}, {"code": f"PowerState/{power}"}]}}}
    v["properties"].update(over)
    return v


def disk_json(k: int, size: int = UNIT) -> dict:
    return {"id": disk_id(k), "name": disk_id(k).rsplit("/", 1)[-1], "managedBy": VM_ID, "sku": {"name": "Premium_LRS"},
            "properties": {"diskSizeBytes": size, "hyperVGeneration": "V2" if k == 0 else None,
                           "osType": "Linux" if k == 0 else None, "encryption": {"type": "EncryptionAtRestWithPlatformKey"}}}


def nic_json(name: str, primary: bool) -> dict:
    return {"id": f"{RG}/providers/Microsoft.Network/networkInterfaces/{name}",
            "properties": {"primary": primary, "macAddress": "00-0D-3A-11-22-3" + ("3" if primary else "4"),
                           "enableAcceleratedNetworking": primary,
                           "ipConfigurations": [{"properties": {
                               "primary": True, "privateIPAddress": "10.1.0.4" if primary else "10.2.0.4",
                               "subnet": {"id": f"{RG}/providers/Microsoft.Network/virtualNetworks/vnet-prod/subnets/"
                                                + ("app" if primary else "backup")}}}]}}


SIZE = {"name": "Standard_D4s_v5", "numberOfCores": 4, "memoryInMB": 16384, "resourceDiskSizeInMB": 0}


def _md5(b: bytes) -> str:
    return base64.b64encode(hashlib.md5(b).digest()).decode()  # noqa: S324


class FakeAzure:
    """Callable HTTP layer: (method, url, headers, body, timeout) -> (status, headers, body)."""

    def __init__(self, n: int = N, power: str = "deallocated"):
        self.n, self.power = n, power
        self.gen = 0
        self.snaps: dict[str, dict] = {}       # snapshot path (lower) -> {disk, gen, tags, polls}
        self.sas: dict[str, str] = {}          # sig -> snapshot path; revoked/expired sigs are removed
        self.ops: dict[str, dict] = {}
        self.k = 0
        self.calls: list[tuple[str, str]] = []
        self.throttle = 0                      # next N ARM calls get 429
        self.blob_errors = 0                   # next N blob range GETs get 503
        self.corrupt = 0                       # next N blob range GETs get a bad MD5
        self.deleted: list[str] = []
        self.revoked: list[str] = []
        self.powered_off = False

    # -------------------------------------------------------------- helpers
    def content(self, k: int, gen: int) -> bytes:
        return PatternDisk(f"RIS-Sainath:{k}", UNIT, generation=gen).read(0, UNIT)

    def op(self, result: dict | None = None, kind: str = "location") -> dict[str, str]:
        self.k += 1
        url = f"{ARM}/operations/op{self.k}"
        self.ops[url] = {"result": result or {}, "polls": 0}
        return {"location": url} if kind == "location" else {"azure-asyncoperation": url}

    def ok(self, d: dict, status: int = 200, headers: dict | None = None):
        return status, headers or {}, json.dumps(d).encode()

    # -------------------------------------------------------------- dispatch
    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url))
        u = urllib.parse.urlsplit(url)
        if u.netloc == "login.microsoftonline.com":
            form = urllib.parse.parse_qs(body.decode())
            if form["client_secret"] != ["s3cret"]:
                return 401, {}, json.dumps({"error": "invalid_client",
                                            "error_description": "AADSTS7000215: Invalid client secret"}).encode()
            return self.ok({"access_token": "tok", "expires_in": 3600})
        if u.netloc.endswith(".blob.core.windows.net"):
            return self.blob(method, u, headers)
        assert headers.get("Authorization") == "Bearer tok"
        if self.throttle:
            self.throttle -= 1
            return 429, {"retry-after": "1"}, b'{"error":{"code":"TooManyRequests","message":"slow down"}}'
        if url in self.ops:
            o = self.ops[url]
            o["polls"] += 1
            if o["polls"] < 2:
                return 202, {}, b""
            return self.ok({"status": "Succeeded"} if "op" in url and o["result"] == {} else o["result"])
        return self.arm(method, u.path, urllib.parse.parse_qs(u.query), json.loads(body) if body else None)

    def arm(self, method, path, q, body):
        p = path.lower()
        assert "api-version" in q or "/operations/" in p
        if method == "GET" and p == SUB.lower():
            return self.ok({"subscriptionId": "sub-1", "state": "Enabled"})
        if method == "GET" and p == f"{SUB}/providers/Microsoft.Compute/virtualMachines".lower():
            if "$skiptoken" in q:
                other = {"id": f"{SUB}/resourceGroups/rg-dev/providers/Microsoft.Compute/virtualMachines/RIS-Sainath",
                         "name": "RIS-Sainath"}
                return self.ok({"value": [other, {"id": f"{RG}/providers/Microsoft.Compute/virtualMachines/web1",
                                                  "name": "web1"}]})
            return self.ok({"value": [vm_json(self.n, self.power)],
                            "nextLink": f"{ARM}{SUB}/providers/Microsoft.Compute/virtualMachines?api-version=x&$skiptoken=2"})
        if method == "GET" and p == f"{RG}/providers/Microsoft.Compute/virtualMachines".lower():
            return self.ok({"value": [vm_json(self.n, self.power)]})
        if method == "GET" and p == VM_ID.lower():
            return self.ok(vm_json(self.n, "stopped" if self.powered_off else self.power))
        if method == "POST" and p == f"{VM_ID}/powerOff".lower():
            self.powered_off = True
            return 202, self.op(kind="async"), b""
        if method == "GET" and p == f"{SUB}/providers/Microsoft.Compute/disks".lower():
            return self.ok({"value": [disk_json(k) for k in range(self.n)] +
                                     [{"id": f"{RG}/providers/Microsoft.Compute/disks/other", "managedBy": None}]})
        m = re.match(r".*/disks/ris-sainath-(os|data(\d+))$", p)
        if method == "GET" and m:
            return self.ok(disk_json(0 if m.group(1) == "os" else int(m.group(2))))
        if method == "GET" and "/networkinterfaces/" in p:
            name = p.rsplit("/", 1)[-1]
            return self.ok(nic_json(name, primary=name == "nic-a"))
        if method == "GET" and p.endswith("/locations/westeurope/vmsizes"):
            return self.ok({"value": [{"name": "Standard_B2s"}, SIZE]})
        if "/snapshots/" in p:
            return self.snapshot(method, p, body)
        raise AssertionError(f"unexpected ARM call {method} {path}")

    def snapshot(self, method, p, body):
        action = None
        if p.endswith(("/begingetaccess", "/endgetaccess")):
            p, action = p.rsplit("/", 1)
        s = self.snaps.get(p)
        if method == "PUT":
            assert body["properties"]["incremental"] is True and body["tags"]["vmigrate:run"].startswith("vmigrate-")
            src = body["properties"]["creationData"]["sourceResourceId"].lower()
            k = next(k for k in range(self.n) if disk_id(k).lower() == src)
            self.snaps[p] = {"disk": k, "gen": self.gen, "tags": body["tags"], "polls": 0, "sas": 0}
            return self.ok({"name": p.rsplit("/", 1)[-1], "properties": {"provisioningState": "Creating"}}, 201)
        if s is None:
            return 404, {}, b'{"error":{"code":"ResourceNotFound","message":"not found"}}'
        if method == "GET":
            s["polls"] += 1
            done = s["polls"] > 1
            return self.ok({"id": p, "tags": s["tags"], "properties": {
                "provisioningState": "Succeeded" if done else "Creating", "completionPercent": 100 if done else 40}})
        if method == "POST" and action == "begingetaccess":
            assert body["access"] == "Read"
            self.k += 1
            sig = f"sig{self.k}"
            self.sas[sig] = p
            s["sas"] += 1
            sas = f"https://md-x1.blob.core.windows.net/{p.rsplit('/', 1)[-1]}/abcd?sv=2021&sr=b&sig={sig}"
            return 202, self.op({"accessSAS": sas}), b""
        if method == "POST" and action == "endgetaccess":
            for sig in [g for g, sp in self.sas.items() if sp == p]:
                del self.sas[sig]
            self.revoked.append(p)
            s["sas"] = 0
            return 202, self.op(kind="async"), b""
        if method == "DELETE":
            assert not any(sp == p for sp in self.sas.values()), "deleted with an active SAS"
            del self.snaps[p]
            self.deleted.append(p)
            return 202, self.op(), b""
        raise AssertionError(f"unexpected snapshot call {method} {p} {action}")

    # -------------------------------------------------------------- page blob
    def _pages(self, data: bytes) -> list[int]:
        return [i for i in range(len(data) // PAGE) if any(data[i * PAGE:(i + 1) * PAGE])]

    def blob(self, method, u, headers):
        q = urllib.parse.parse_qs(u.query)
        snap = self.sas.get(q.get("sig", [""])[0])
        if snap is None:
            return 403, {"x-ms-error-code": "AuthenticationFailed"}, b"<Error>Signature not valid</Error>"
        s = self.snaps[snap]
        data = self.content(s["disk"], s["gen"])
        if method == "HEAD":
            return 200, {"content-length": str(len(data))}, b""
        a, b = (int(x) for x in headers["x-ms-range"].removeprefix("bytes=").split("-"))
        if q.get("comp") == ["pagelist"]:
            ranges = []
            prev = headers.get("x-ms-previous-snapshot-url")
            if prev:
                ps = self.snaps[self.sas[urllib.parse.parse_qs(urllib.parse.urlsplit(prev).query)["sig"][0]]]
                assert ps["disk"] == s["disk"], "diff across different disks"
                old = self.content(ps["disk"], ps["gen"])
                for i in range(a // PAGE, (b + 1) // PAGE):
                    x, y = old[i * PAGE:(i + 1) * PAGE], data[i * PAGE:(i + 1) * PAGE]
                    if x != y:
                        ranges.append(("ClearRange" if not any(y) else "PageRange", i))
            else:
                ranges = [("PageRange", i) for i in self._pages(data) if a // PAGE <= i <= b // PAGE]
            xml = "".join(f"<{t}><Start>{i * PAGE}</Start><End>{(i + 1) * PAGE - 1}</End></{t}>" for t, i in ranges)
            return 200, {}, f'<?xml version="1.0"?><PageList>{xml}</PageList>'.encode()
        if self.blob_errors:
            self.blob_errors -= 1
            return 503, {}, b"ServerBusy"
        chunk = data[a:b + 1]
        md5 = _md5(chunk)
        if self.corrupt:
            self.corrupt -= 1
            chunk = b"\xff" + chunk[1:]
        return 206, ({"content-md5": md5} if headers.get("x-ms-range-get-content-md5") else {}), chunk


def client(fake: FakeAzure, **kw) -> AzureClient:
    return AzureClient(subscription_id="sub-1", tenant_id="t-1", client_id="c-1", client_secret=kw.pop("secret", "s3cret"),
                       http=fake, poll=0, sleep=lambda s: None, **kw)


# ---------------------------------------------------------------------- mapping

def _disks(n: int = N) -> dict:
    return {disk_id(k): disk_json(k) for k in range(n)}


def _nics() -> list:
    return [nic_json("nic-b", False), nic_json("nic-a", True)]


def test_mapping_13_disks_gen2_trusted_launch():
    spec = mapping.to_vmspec(vm_json(), _disks(), _nics(), SIZE)
    assert len(spec.disks) == 13 and spec.boot_disk().label == "os"
    assert [d.label for d in spec.ordered_disks()][:3] == ["os", "lun0", "lun1"] and spec.ordered_disks()[12].label == "lun11"
    assert {d.controller for d in spec.disks} == {"storvsc"} and spec.disks[0].bus == DiskBus.SCSI
    assert spec.firmware == Firmware.UEFI and spec.secure_boot and spec.cpu.total == 4 and spec.memory_bytes == 16 << 30
    assert spec.guest_os.family == OsFamily.LINUX and spec.guest_os.distro == "rhel"
    assert spec.nics[0].mac == "00:0d:3a:11:22:33" and spec.nics[0].network == "vnet-prod/app"   # primary first
    assert spec.nics[0].ipv4 == "10.1.0.4" and spec.nics[0].model == "netvsc+mlx"
    assert check_disk_counts(spec, mapping.crosscheck(vm_json(), list(_disks().values())))
    with pytest.raises(DiskCountMismatch):
        check_disk_counts(spec, mapping.crosscheck(vm_json(), list(_disks().values())[:10]))


def test_mapping_refusals_and_variants():
    gen1 = {**_disks(), disk_id(0): {**disk_json(0), "properties": {**disk_json(0)["properties"], "hyperVGeneration": "V1"}}}
    v = vm_json()
    v["properties"]["storageProfile"]["osDisk"]["osType"] = "Windows"
    v["properties"]["storageProfile"]["diskControllerType"] = "NVMe"
    s = mapping.to_vmspec(v, gen1, _nics(), SIZE)
    assert s.firmware == Firmware.BIOS and not s.secure_boot and s.guest_os.family == OsFamily.WINDOWS
    assert s.disks[0].controller == "nvme" and s.disks[0].bus == DiskBus.NVME
    with pytest.raises(mapping.AzureMappingError, match="Confidential"):
        mapping.to_vmspec(vm_json(securityProfile={"securityType": "ConfidentialVM"}), _disks(), _nics(), SIZE)
    eph = vm_json()
    eph["properties"]["storageProfile"]["osDisk"]["diffDiskSettings"] = {"option": "Local"}
    with pytest.raises(mapping.AzureMappingError, match="ephemeral"):
        mapping.to_vmspec(eph, _disks(), _nics(), SIZE)
    vhd = vm_json()
    vhd["properties"]["storageProfile"]["dataDisks"][0] = {"lun": 11, "name": "old", "vhd": {"uri": "https://x/y.vhd"}}
    with pytest.raises(mapping.AzureMappingError, match="unmanaged"):
        mapping.to_vmspec(vhd, _disks(), _nics(), SIZE)
    with pytest.raises(mapping.AzureMappingError, match="not returned"):
        mapping.to_vmspec(vm_json(), _disks(12), _nics(), SIZE)
    shared = {**_disks(), disk_id(5): {**disk_json(5), "properties": {**disk_json(5)["properties"], "maxShares": 2}}}
    assert mapping.to_vmspec(vm_json(), shared, _nics(), SIZE).disks[5].shared_id == disk_id(5).lower()


# ---------------------------------------------------------------------- client + blob

def test_client_signin_paging_throttle_and_bad_secret():
    fake = FakeAzure()
    c = client(fake)
    fake.throttle = 2
    names = [v["name"] for v in c.vms()]
    assert names == ["RIS-Sainath", "RIS-Sainath", "web1"]          # nextLink followed after two 429s
    with pytest.raises(PermissionError, match="AADSTS7000215"):
        client(FakeAzure(), secret="wrong").vms()


def test_reader_md5_retry_sas_renewal_and_allocation():
    fake = FakeAzure()
    c = client(fake)
    sid = c.start_snapshot("rg-prod", "vmigrate-t-03", "westeurope", disk_id(3), "vmigrate-t")
    c.wait_snapshot(sid)
    grants = [c.grant_access(sid, 3600)]

    def sas(renew):
        if renew:
            grants.append(c.grant_access(sid, 3600))
        return grants[-1]

    r = SasSnapshotReader(sas, UNIT, http=fake, workers=4, sleep=lambda s: None)
    want = fake.content(3, 0)
    fake.blob_errors, fake.corrupt = 2, 2
    assert r.read(0, UNIT) == want                                   # 503s retried, bad MD5 re-fetched
    fake.sas.clear()                                                 # SAS expired / revoked
    assert r.read(5 << 20, 1 << 20) == want[5 << 20:]
    assert len(grants) == 2
    assert [(x.offset, x.length) for x in r.allocated_ranges()] == [(0, 4 << 20)]   # 4-6 MiB never written
    r.close()


def test_unowned_snapshot_is_never_touched():
    fake = FakeAzure()
    c = client(fake)
    p = f"{RG}/providers/Microsoft.Compute/snapshots/nightly".lower()
    fake.snaps[p] = {"disk": 0, "gen": 0, "tags": {"owner": "backup"}, "polls": 5, "sas": 0}
    with pytest.raises(SafetyGateError, match="not tagged"):
        c.delete_snapshot(p)
    with pytest.raises(SafetyGateError):
        c.grant_access(p, 60)
    assert p in fake.snaps
    with pytest.raises(SafetyGateError):
        c.start_snapshot("rg-prod", "nightly-2", "westeurope", disk_id(0), "vmigrate-x")


# ---------------------------------------------------------------------- engine runs

def make(tmp_path, fake, **cfg):
    ctx = RunContext(dry_run=False, logger=logging.getLogger("t"))
    src = AzureSource("az", {"type": "azure", "subscription_id": "sub-1", **cfg}, ctx, client=client(fake))
    tgt = FakeTarget("tgt", {"staging_dir": str(tmp_path / "tgt"), "persist": True}, ctx)
    e = EngineConfig(state_dir=tmp_path / "s", report_dir=tmp_path / "r", chunk_size=1 << 20, checkpoint_every=2 << 20,
                     verify_samples=4, verify_sample_bytes=64 << 10)
    return ctx, src, tgt, e


def test_registry_and_find_vm(tmp_path):
    with pytest.raises(Exception, match="subscription_id"):
        build_source("az", {"type": "azure"}, RunContext(dry_run=True, logger=logging.getLogger("t")))
    fake = FakeAzure()
    _, src, _, _ = make(tmp_path, fake)
    assert [v.name for v in src.list_vms()] == ["rg-prod/RIS-Sainath", "rg-dev/RIS-Sainath", "web1"]
    with pytest.raises(SafetyGateError, match="rg-prod/RIS-Sainath"):
        src.find_vm("RIS-Sainath")
    assert src.find_vm("rg-prod/RIS-Sainath").id == VM_ID and src.find_vm(VM_ID.upper()).id == VM_ID
    assert src.check_connection() == []


def test_migration_reads_snapshots_and_cleans_up(tmp_path):
    fake = FakeAzure()
    ctx, src, tgt, e = make(tmp_path, fake)
    job = MigrationConfig(name="m", source="az", target="tgt", vm="rg-prod/RIS-Sainath", app_consistent=False,
                          osmorph={"allow_manual": True})
    st = MigrationRunner(job, e, src, tgt, ctx, StateStore(e.state_dir)).run()
    assert st.phase.value == "done" and st.morph["status"] == "manual_required"   # storvsc -> virtio required
    for k in range(N):
        assert tgt._final(tgt.disks[f"RIS-Sainath-disk{k:02d}"]).read_bytes() == fake.content(k, 0)
    assert len(fake.deleted) == N and not fake.snaps and not fake.sas          # deleted, no SAS left open
    assert all(d["copy_stats"]["bytes_read"] < UNIT for d in (x.__dict__ for x in st.disks.values()))  # stripe skipped


def test_running_multi_disk_vm_needs_opt_in(tmp_path):
    fake = FakeAzure(power="running")
    ctx, src, tgt, e = make(tmp_path, fake)
    job = MigrationConfig(name="m", source="az", target="tgt", vm="rg-prod/RIS-Sainath", app_consistent=False,
                          osmorph={"allow_manual": True})
    with pytest.raises(SafetyGateError, match="allow_per_disk_snapshots"):
        MigrationRunner(job, e, src, tgt, ctx, StateStore(e.state_dir)).run()
    assert not fake.snaps
    with pytest.raises(SafetyGateError, match="crash-consistent"):
        src.create_snapshot(src.find_vm("rg-prod/RIS-Sainath"), True)
    ctx, src, tgt, e = make(tmp_path / "2", fake, allow_per_disk_snapshots=True)
    assert MigrationRunner(job, e, src, tgt, ctx, StateStore(e.state_dir)).run().phase.value == "done"
    single = FakeAzure(n=1, power="running")
    ctx, src, tgt, e = make(tmp_path / "3", single)
    assert MigrationRunner(job, e, src, tgt, ctx, StateStore(e.state_dir)).run().phase.value == "done"


def test_page_range_diff_replica(tmp_path):
    fake, store = FakeAzure(), MemoryReplicaStore()

    def sync():
        ctx, src, tgt, e = make(tmp_path, fake)
        rr = ReplicaRunner(MigrationConfig(name="rep", source="az", target="tgt", vm="rg-prod/RIS-Sainath",
                                           app_consistent=False), e, src, tgt, ctx, store)
        ts = MemoryTaskStore(sync_dag(False))
        assert DagRunner(ts, rr.sync_handlers()).run().value == "completed", [t.error for t in ts.load_tasks() if t.error]
        return rr, tgt

    sync()
    assert len(fake.snaps) == N and not fake.sas                   # first set kept as the diff base, access ended
    fake.gen = 1
    rr, tgt = sync()
    last = rr.state.syncs[-1]
    assert last["modes"] == ["cbt"] and last["changed_bytes"] < N * UNIT
    assert last["changed_bytes"] <= last["bytes_read"] <= last["changed_bytes"] * 1.5 + (N << 20)
    assert len(fake.deleted) == N and len(fake.snaps) == N and not fake.sas   # base rotated
    for k in range(N):
        assert tgt._final(tgt.disks[f"rep-r{k:02d}"]).read_bytes() == fake.content(k, 1)


def test_page_ranges_diff_reports_cleared_pages():
    fake = FakeAzure(n=1)
    c = client(fake)
    a = c.start_snapshot("rg-prod", "vmigrate-a-00", "westeurope", disk_id(0), "vmigrate-a")
    fake.gen = 1
    b = c.start_snapshot("rg-prod", "vmigrate-b-00", "westeurope", disk_id(0), "vmigrate-b")
    old, new = fake.content(0, 0), fake.content(0, 1)
    changed = {i for i in range(UNIT // PAGE) if old[i * PAGE:(i + 1) * PAGE] != new[i * PAGE:(i + 1) * PAGE]}
    got = page_ranges(fake, c.grant_access(b, 60), UNIT, previous=c.grant_access(a, 60))
    covered = {i for r in got for i in range(r.offset // PAGE, r.end // PAGE)}
    assert covered == changed and changed


def test_cutover_power_off(tmp_path):
    fake = FakeAzure(power="running")
    ctx, src, _, _ = make(tmp_path, fake)
    vm = src.find_vm("rg-prod/RIS-Sainath")
    assert src.shutdown_vm(vm) == "RIS-Sainath stopped" and fake.powered_off
    assert src.shutdown_vm(vm) == "RIS-Sainath already stopped"
