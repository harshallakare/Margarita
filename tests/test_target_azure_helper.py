"""Purpose: Azure target helper_vm mode (Coriolis' documented Azure approach): empty managed disks attached to a
helper VM and written through the REAL NBD agent (run locally against per-disk files), replica resync in place,
incremental-snapshot restore points, deploy from snapshots (boot disk gets osType + generation), migration with the
boot-disk copy, OSMorph strategy selection, cleanup.

Usage: pytest tests/test_target_azure_helper.py
"""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from vmigrate.config.loader import EngineConfig, MigrationConfig
from vmigrate.core.context import RunContext
from vmigrate.core.errors import SafetyGateError
from vmigrate.core.interfaces import DiskHints
from vmigrate.engine import MigrationRunner, Phase, StateStore
from vmigrate.ir import CpuSpec, DiskBus, DiskSpec, Firmware, GuestOS, NicSpec, OsFamily, VmSpec
from vmigrate.osmorph import MorphRequest, resolve
from vmigrate.providers.azure import target as T
from vmigrate.providers.fake.provider import FakeSource

from .conftest import fake_vm
from .test_vhi_helper import fake_keygen

SUB = "/subscriptions/sub-1"
RG = f"{SUB}/resourceGroups/rg-mig"
SUBNET = f"{SUB}/resourceGroups/net/providers/Microsoft.Network/virtualNetworks/vnet/subnets/migration"
APP = f"{SUB}/resourceGroups/net/providers/Microsoft.Network/virtualNetworks/vnet/subnets/app"
HELPER = f"{RG}/providers/Microsoft.Compute/virtualMachines/vmigrate-helper-run-abcdef12"


class FakeArm:
    def __init__(self, root: Path):
        self.sub, self.root = "sub-1", root
        root.mkdir(parents=True, exist_ok=True)
        self.res: dict[str, dict] = {RG: {"id": RG}, SUBNET: {"id": SUBNET}, APP: {"id": APP}}
        self.log: list[str] = []
        self.http = None

    def f(self, path): return self.root / path.rsplit("/", 1)[-1]
    def subscription(self): return {}
    def get_or_none(self, path, api): return self.res.get(path)
    def get(self, path, api):
        if path.endswith("/instanceView"):
            return {"statuses": [{"code": "PowerState/running"}], "vmAgent": {"statuses": [{"displayStatus": "Ready"}]}}
        return self.res[path]
    def list(self, path, api):
        return [{"name": "Standard_D2s_v5", "numberOfCores": 2, "memoryInMB": 8192}]
    def wait_async(self, h, what): return {}
    def wait_snapshot(self, path): return self.res[path]

    def helper_luns(self) -> dict[int, str]:
        vm = self.res.get(HELPER)
        if not vm:
            return {}
        return {d["lun"]: d["managedDisk"]["id"] for d in vm["properties"]["storageProfile"]["dataDisks"]}

    def request(self, method, path, *, api=None, body=None, ok=(200,)):
        if method == "DELETE":
            self.log.append(f"delete:{path.rsplit('/', 1)[-1]}")
            r = self.res.pop(path, None)
            if r and "/disks/" in path:
                assert path not in self.helper_luns().values(), "deleting a disk still attached to the helper"
            self.f(path).unlink(missing_ok=True)
            return 200, {}, {}
        assert method == "PUT", (method, path)
        r = {"id": path, **body}
        if "/disks/" in path or "/snapshots/" in path:
            cd = body["properties"]["creationData"]
            if cd["createOption"] == "Empty":
                with open(self.f(path), "wb") as fh:
                    fh.truncate(body["properties"]["diskSizeGB"] << 30)
            elif cd["createOption"] == "Copy":
                assert cd["sourceResourceId"] in self.res, cd
                shutil.copyfile(self.f(cd["sourceResourceId"]), self.f(path))
            r["properties"] = {**body["properties"], "provisioningState": "Succeeded", "diskState": "Unattached"}
            self.log.append(f"put:{path.rsplit('/', 2)[-2]}:{path.rsplit('/', 1)[-1]}:{cd['createOption']}")
        if "/networkInterfaces/" in path:
            r["properties"]["ipConfigurations"][0]["properties"]["privateIPAddress"] = "10.9.0.4"
        if "/virtualMachines/" in path and path != HELPER:
            sp = body["properties"]["storageProfile"]
            for ref in [sp["osDisk"], *sp["dataDisks"]]:
                assert ref["managedDisk"]["id"] not in self.helper_luns().values(), "disk still on the helper"
            self.log.append("create-vm")
        if path == HELPER and HELPER not in self.res:
            self.log.append("create-helper")
        self.res[path] = r
        return 201, {"azure-asyncoperation": "op"}, r


def transport(arm: FakeArm):
    class Local:
        def __init__(self, host, key, known, port=22):
            assert host == "10.9.0.4" and Path(known).read_text().startswith("vmigrate-helper ssh-ed25519")

        def run(self, cmd, timeout=120):
            if cmd.startswith("readlink -f /dev/disk/azure/scsi1/lun"):
                lun = int(cmd.split("lun")[1].split()[0])
                return f"/dev/sd{chr(99 + lun)}\n" if lun in arm.helper_luns() else ""
            if cmd.startswith("command -v python3"):
                return "/usr/bin/python3\n"
            return ""

        def popen(self, cmd):
            a = shlex.split(cmd)
            assert a[:4] == ["sudo", "-n", "python3", "-c"]
            lun = ord(a[5][-1]) - 99
            return subprocess.Popen([sys.executable, "-c", a[4], str(arm.f(arm.helper_luns()[lun])), a[6]],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    return Local


def _tgt(tmp_path, ctx=None, **cfg):
    ctx = ctx or RunContext(dry_run=False, logger=logging.getLogger("azh"), run_id="run-abcdef123456")
    arm = FakeArm(tmp_path / "disks")
    conf = {"type": "azure", "subscription_id": "sub-1", "location": "westeurope", "target_resource_group": "rg-mig",
            "helper_subnet": SUBNET, "network_map": {"net0": APP}, "default_subnet": APP, "poll_interval": 0,
            "staging_dir": str(tmp_path / "staging"), **cfg}
    t = T.AzureTarget("az", conf, ctx, client=arm)
    t._helper_kw = {"transport_factory": transport(arm), "keygen": fake_keygen}
    return t, arm


def test_auto_mode_and_flags(tmp_path):
    t, _ = _tgt(tmp_path)
    assert t.write_mode == "helper_vm" and t.supports_replica() and t.supports_restore_points()[0]
    assert t.morph_options() == {"azure_disks_editable": True}
    up, _ = _tgt(tmp_path / "u", helper_subnet=None)
    assert up.write_mode == "upload" and not up.supports_replica()


def test_full_migration_through_helper(tmp_path):
    ctx = RunContext(dry_run=False, logger=logging.getLogger("azh-e2e"), run_id="run-abcdef123456")
    src = FakeSource("src", {"type": "fake", "vms": [fake_vm(disks=3, size="3MiB")]}, ctx)
    t, arm = _tgt(tmp_path, ctx)
    eng = EngineConfig(state_dir=tmp_path / "state", report_dir=tmp_path / "rep", chunk_size=1 << 20,
                       verify_samples=4, verify_sample_bytes=64 << 10, boot_timeout=5, max_parallel_disks=2,
                       verify_full=True)
    job = MigrationConfig(name="m", source="src", target="az", vm="RIS-Sainath", osmorph={"allow_manual": True},
                          target_options={"network_map": {}})
    st = MigrationRunner(job, eng, src, t, ctx, StateStore(eng.state_dir)).run()
    t.close()
    assert st.phase == Phase.DONE, st.errors
    assert all(d.verify["full"]["ok"] for d in st.disks.values())
    vm = arm.res[f"{RG}/providers/Microsoft.Compute/virtualMachines/RIS-Sainath"]
    sp = vm["properties"]["storageProfile"]
    os_disk = arm.res[sp["osDisk"]["managedDisk"]["id"]]
    assert os_disk["properties"]["osType"] == "Linux" and os_disk["properties"]["hyperVGeneration"] == "V1"
    assert len(sp["dataDisks"]) == 2
    assert arm.log.count("create-helper") == 1 and HELPER not in arm.res          # helper removed
    assert not any("/snapshots/" in k for k in arm.res)                          # temporary snapshot removed
    written_boot = vm["tags"]["vmigrate:boot-of"]
    assert written_boot not in arm.res                                            # replaced by its copy
    assert st.verification["boot"]["ok"]


def test_replica_resync_restore_point_and_deploy(tmp_path):
    t, arm = _tgt(tmp_path)
    d = t.create_disk("rep-r00", 1 << 20, DiskHints(bus=DiskBus.SCSI, is_boot=True, order=0, firmware="uefi",
                                                    os_family="windows"))
    with t.open_disk_writer(d) as w:
        assert w.prezeroed
        w.write(0, b"1" * 4096)
    rp = t.snapshot_disk(d, "rp-1")
    with t.open_disk_writer(d, incremental=True) as w:                           # resync in place
        assert not w.prezeroed
        w.write(0, b"2" * 4096)
    with t.open_disk_reader(d) as r:
        assert r.read(0, 4) == b"2222"
    clone = t.clone_disk(d, "deploy-os", restore_point=rp)
    props = arm.res[clone.id]["properties"]
    assert props["osType"] == "Windows" and props["hyperVGeneration"] == "V2"
    assert arm.f(clone.id).read_bytes()[:4] == b"1111"                            # the restore point's content
    spec = VmSpec(name="dr-web", source_id="s", cpu=CpuSpec(1, 2), memory_bytes=4 << 30,
                  disks=[DiskSpec(id="x", bus=DiskBus.SCSI, size_bytes=1 << 20, order=0, is_boot=True)],
                  nics=[NicSpec(id="n", network="net0")], firmware=Firmware.UEFI, guest_os=GuestOS(OsFamily.WINDOWS))
    vm = t.create_vm(spec, clone, [])
    assert t.list_attached_disks(vm) == [clone.id] and d.id in arm.res            # the replica stays
    t.delete_disk_snapshot(d, rp)
    assert rp not in arm.res
    t.close()
    t.delete_disk(d)
    assert d.id not in arm.res


def test_osmorph_selection_and_cleanup(tmp_path):
    spec = VmSpec(name="w", source_id="s", cpu=CpuSpec(1, 1), memory_bytes=1 << 30,
                  disks=[DiskSpec(id="d", bus=DiskBus.SCSI, size_bytes=1, order=0, is_boot=True, controller="pvscsi")],
                  nics=[NicSpec(id="n", network="net0", model="vmxnet3")], firmware=Firmware.BIOS,
                  guest_os=GuestOS(OsFamily.LINUX))
    img = [("nbd://?socket=/x", "raw")]
    names = [s.name for s in resolve(OsFamily.LINUX, "azure").plan(MorphRequest(spec, "azure", DiskBus.SCSI, img,
                                                                                {"azure_disks_editable": True}))]
    assert "inject-hyperv-initramfs" in names and "azure-guest-ready" not in names
    gate = [s.name for s in resolve(OsFamily.LINUX, "azure").plan(MorphRequest(spec, "azure", DiskBus.SCSI, [], {}))]
    assert gate[0] == "azure-guest-ready"                                          # upload mode: prerequisites only
    from vmigrate.osmorph import linux_guest as lg
    assert lg.tools_to_remove(["open-vm-tools", "hyperv-daemons", "walinuxagent"], keep=("hyperv", "azure")) == \
        ["open-vm-tools"]
    t, arm = _tgt(tmp_path)
    assert t.preflight(spec) == []                     # no driver gate: the helper lets OSMorph edit the guest
    d = t.create_disk("m-disk00", 1 << 20, DiskHints(bus=DiskBus.SCSI, is_boot=True))
    with t.open_disk_writer(d) as w:
        w.write(0, b"x" * 512)
    assert HELPER in arm.res
    arm.res[f"{RG}/providers/Microsoft.Compute/disks/prod"] = {"id": "p", "tags": {}}
    with pytest.raises(SafetyGateError):
        t.cleanup_on_failure({"target_disks": [{"id": "p", "name": "prod", "size_bytes": 1,
                                                "data": {"path": f"{RG}/providers/Microsoft.Compute/disks/prod"}}]})
    t2, _ = _tgt(tmp_path)
    t2._client, t2._helper = arm, None
    t2._helper_kw = t._helper_kw
    t2.cleanup_on_failure({"target_disks": [{"id": d.id, "name": d.name, "size_bytes": 1, "data": d.data}]})
    assert d.id not in arm.res
