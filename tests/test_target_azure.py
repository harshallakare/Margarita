"""Purpose: Azure target: upload managed disks through a fake ARM + blob endpoint (Put Page, VHD footer, write/read
SAS), VM + NICs creation, the cloud prerequisite gate, interrupted-upload recreation, cleanup safety.

Usage: pytest tests/test_target_azure.py
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re

import pytest

from vmigrate.config.loader import EngineConfig, MigrationConfig
from vmigrate.core.context import RunContext
from vmigrate.core.errors import ProviderError, SafetyGateError
from vmigrate.core.formats.vhd import vhd_checksum
from vmigrate.core.interfaces import DiskHints
from vmigrate.engine import MigrationRunner, Phase, StateStore
from vmigrate.ir import CpuSpec, DiskBus, DiskSpec, Firmware, GuestOS, NicSpec, OsFamily, VmSpec
from vmigrate.providers import TARGETS
from vmigrate.providers.azure import target as T
from vmigrate.providers.azure import upload as U
from vmigrate.providers.fake.provider import FakeSource

from .conftest import fake_vm

SUB = "/subscriptions/sub-1"
RG = f"{SUB}/resourceGroups/rg-mig"
SUBNET = f"{SUB}/resourceGroups/net/providers/Microsoft.Network/virtualNetworks/vnet/subnets/app"


class FakeArm:
    def __init__(self):
        self.sub = "sub-1"
        self.res: dict[str, dict] = {RG: {"id": RG, "tags": {}}, SUBNET: {"id": SUBNET, "tags": {}},
                                     f"{RG}/providers/Microsoft.Compute/disks/prod-os": {"id": "x", "tags": {}}}
        self.blobs: dict[str, bytearray] = {}        # disk path -> bytes
        self.sas: dict[str, tuple[str, str]] = {}    # sas url -> (disk path, access)
        self.log: list[str] = []
        self.n = 0
        self.http = self._http

    # ---- ARM
    def subscription(self): return {"subscriptionId": "sub-1"}
    def get_or_none(self, path, api): return self.res.get(path)
    def get(self, path, api):
        if path.endswith("/instanceView"):
            return {"statuses": [{"code": "PowerState/running"}], "vmAgent": {"statuses": [{"displayStatus": "Ready"}]}}
        return self.res[path]
    def list(self, path, api):
        if path.endswith("/vmSizes"):
            return [{"name": "Standard_D2s_v5", "numberOfCores": 2, "memoryInMB": 8192},
                    {"name": "Standard_D4s_v5", "numberOfCores": 4, "memoryInMB": 16384},
                    {"name": "Standard_B2s", "numberOfCores": 2, "memoryInMB": 4096}]
        return [{"name": "vnet", "properties": {"subnets": [{"id": SUBNET, "name": "app",
                                                              "properties": {"addressPrefix": "10.1.0.0/24"}}]}}]
    def wait_async(self, h, what): return {}

    def request(self, method, path, *, api=None, body=None, ok=(200,)):
        if method == "PUT":
            self.log.append(f"put:{path.rsplit('/', 2)[-2]}")
            r = {"id": path, **body}
            if "/disks/" in path:
                assert body["properties"]["creationData"]["createOption"] == "Upload"
                r["properties"] = {**body["properties"], "diskState": "ReadyToUpload"}
                self.blobs[path] = bytearray(body["properties"]["creationData"]["uploadSizeBytes"])
            if "/virtualMachines/" in path:
                sp = body["properties"]["storageProfile"]
                for ref in [sp["osDisk"], *sp["dataDisks"]]:
                    d = self.res[ref["managedDisk"]["id"]]
                    assert d["properties"]["diskState"] == "Unattached", d["properties"]["diskState"]
                    d["properties"]["diskState"] = "Attached"
            self.res[path] = r
            return 201, {"azure-asyncoperation": "op"}, r
        if method == "DELETE":
            self.log.append(f"delete:{path.rsplit('/', 1)[-1]}")
            self.res.pop(path, None)
            self.blobs.pop(path, None)
            return 200, {}, {}
        if method == "POST" and path.endswith("/beginGetAccess"):
            disk = path[: -len("/beginGetAccess")]
            st = self.res[disk]["properties"]
            self.n += 1
            url = f"https://md-xyz.blob.core.windows.net/abc/abcd?sv=1&sig={self.n}"
            self.sas[url] = (disk, body["access"])
            st["diskState"] = "ActiveUpload" if body["access"] == "Write" else "ActiveSAS"
            return 200, {}, {"accessSAS": url}
        if method == "POST" and path.endswith("/endGetAccess"):
            disk = path[: -len("/endGetAccess")]
            st = self.res[disk]["properties"]
            for k in [k for k, v in self.sas.items() if v[0] == disk]:
                self.sas.pop(k)
            st["diskState"] = "Unattached"
            return 200, {}, {}
        if method == "POST" and "retrieveBootDiagnosticsData" in path:
            return 200, {}, {}
        raise AssertionError((method, path))

    # ---- blob data plane
    def _http(self, method, url, headers, body, timeout):
        base = url.split("&comp=")[0]
        if base not in self.sas:
            return 403, {"x-ms-error-code": "AuthenticationFailed"}, b""
        disk, access = self.sas[base]
        blob = self.blobs[disk]
        if method == "PUT":
            assert access == "Write" and "comp=page" in url
            a, b = map(int, re.match(r"bytes=(\d+)-(\d+)", headers["x-ms-range"]).groups())
            assert a % 512 == 0 and (b + 1) % 512 == 0 and len(body) == b - a + 1 <= 4 << 20
            assert base64.b64encode(hashlib.md5(body).digest()).decode() == headers["Content-MD5"]
            blob[a:b + 1] = body
            return 201, {}, b""
        if method == "HEAD":
            return 200, {"content-length": str(len(blob))}, b""
        if method == "GET":
            a, b = map(int, re.match(r"bytes=(\d+)-(\d+)", headers["x-ms-range"]).groups())
            return 206, {}, bytes(blob[a:b + 1])
        raise AssertionError(method)


def _tgt(ctx=None, **cfg):
    ctx = ctx or RunContext(dry_run=False, logger=logging.getLogger("az"), run_id="run-abcdef123456")
    arm = FakeArm()
    conf = {"type": "azure", "subscription_id": "sub-1", "location": "westeurope", "target_resource_group": "rg-mig",
            "network_map": {"net0": SUBNET}, "poll_interval": 0, **cfg}
    return T.AzureTarget("az", conf, ctx, client=arm), arm


def test_vhd_footer_and_helpers(tmp_path):
    size = 3 << 20
    f = U.vhd_footer(size, ident=b"\x01" * 16, now=946684800 + 5)
    assert f[:8] == b"conectix" and len(f) == 512 and int.from_bytes(f[64:68], "big") == vhd_checksum(f)
    from vmigrate.core.formats.vhd import VhdReader
    p = tmp_path / "d.vhd"
    p.write_bytes(bytes(size) + f)
    assert VhdReader(p).size == size                                      # our own VHD reader accepts it
    assert U.chs(127 << 30)[1] == 16
    assert T.pick_size(FakeArm().list("x/vmSizes", ""), 3, 8000) == "Standard_D4s_v5"
    assert T.disk_name("RIS-Sainath disk 00") == "vmigrate-RIS-Sainath-disk-00"
    b = T.disk_body("westeurope", size, "Premium_LRS", DiskHints(bus=DiskBus.SCSI, is_boot=True, firmware="uefi",
                                                                 os_family="windows"), "x")
    assert b["properties"]["creationData"]["uploadSizeBytes"] == size + 512
    assert b["properties"]["osType"] == "Windows" and b["properties"]["hyperVGeneration"] == "V2"


def test_full_migration_to_azure(tmp_path):
    assert TARGETS["azure"][1] == "implemented"
    ctx = RunContext(dry_run=False, logger=logging.getLogger("az-e2e"), run_id="run-abcdef123456")
    src = FakeSource("src", {"type": "fake", "vms": [fake_vm(disks=2, size="3MiB")]}, ctx)
    t, arm = _tgt(ctx, default_subnet=SUBNET)
    eng = EngineConfig(state_dir=tmp_path / "state", report_dir=tmp_path / "rep", chunk_size=1 << 20,
                       verify_samples=4, verify_sample_bytes=64 << 10, boot_timeout=5, max_parallel_disks=2,
                       verify_full=True)
    job = MigrationConfig(name="m", source="src", target="az", vm="RIS-Sainath",
                          osmorph={"cloud_drivers_ready": True}, target_options={"network_map": {}})
    st = MigrationRunner(job, eng, src, t, ctx, StateStore(eng.state_dir)).run()
    assert st.phase == Phase.DONE, st.errors
    assert all(d.verify["full"]["ok"] for d in st.disks.values())
    vm = arm.res[f"{RG}/providers/Microsoft.Compute/virtualMachines/RIS-Sainath"]
    sp = vm["properties"]["storageProfile"]
    assert sp["osDisk"]["createOption"] == "Attach" and len(sp["dataDisks"]) == 1
    assert vm["properties"]["hardwareProfile"]["vmSize"] == "Standard_D2s_v5"
    for path, blob in arm.blobs.items():                                   # every disk ends with a valid footer
        assert blob[-512:-504] == b"conectix" and arm.res[path]["properties"]["diskState"] == "Attached"
    assert not arm.sas                                                     # no SAS left open
    assert st.verification["boot"]["ok"] and "Ready" in st.verification["boot"]["detail"]


def test_gate_and_no_replica():
    t, arm = _tgt()
    spec = VmSpec(name="w", source_id="s", cpu=CpuSpec(1, 1), memory_bytes=1 << 30,
                  disks=[DiskSpec(id="d", bus=DiskBus.SCSI, size_bytes=1 << 20, order=0, is_boot=True)],
                  nics=[NicSpec(id="n", network="net0")], firmware=Firmware.BIOS, guest_os=GuestOS(OsFamily.LINUX))
    assert any("cloud_drivers_ready" in p for p in t.preflight(spec))
    assert not t.supports_replica()
    d = t.create_disk("x", 1 << 20, DiskHints(bus=DiskBus.SCSI))
    with pytest.raises(ProviderError):
        t.open_disk_writer(d, incremental=True)


def test_interrupted_upload_recreates_and_cleanup_safety():
    t, arm = _tgt()
    d = t.create_disk("m-disk00", 2 << 20, DiskHints(bus=DiskBus.SCSI, is_boot=True))
    w = t.open_disk_writer(d)
    assert w.prezeroed and not w.resumable
    w.write(0, b"x" * 4096)
    w.close()
    t._open.clear()                                       # the process died; the write SAS is lost
    t2, _ = _tgt()
    t2._client = arm
    w2 = t2.open_disk_writer(d)                           # ActiveUpload -> revoked, deleted, recreated
    assert f"delete:{T.disk_name('m-disk00')}" in arm.log
    assert bytes(arm.blobs[d.data["path"]][:4096]) == bytes(4096)
    w2.close()
    t2.commit_disk(d, None)
    with pytest.raises(SafetyGateError):
        t2.cleanup_on_failure({"target_disks": [{"id": "x", "name": "prod-os", "size_bytes": 1,
                                                 "data": {"path": f"{RG}/providers/Microsoft.Compute/disks/prod-os"}}]})
    t2.cleanup_on_failure({"target_disks": [{"id": d.id, "name": d.name, "size_bytes": 1, "data": d.data}],
                           "target_vm_pending": "m"})
    assert d.data["path"] not in arm.res
    assert t2.list_networks()[0]["id"] == SUBNET
