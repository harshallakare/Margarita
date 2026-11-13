"""Purpose: XenServer / XCP-ng source tests without a pool. A fake HTTP layer plays XAPI JSON-RPC
(login with HOST_IS_SLAVE redirect, VM/VBD/VDI/VIF records, VM.snapshot, VDI.get_nbd_info,
VDI.list_changed_blocks, VDI.data_destroy, destroy); disk data comes through an injected NBD
connector or the export_raw_vdi stream. The real XenApi and XenSource run on top of it.

Usage: pytest tests/test_xen.py
"""

from __future__ import annotations

import base64
import io
import json
import logging

import pytest

from vmigrate.config.loader import EngineConfig, MigrationConfig
from vmigrate.core.context import RunContext
from vmigrate.core.errors import DiskCountMismatch, SafetyGateError
from vmigrate.core.interfaces import DiskReader
from vmigrate.engine import MigrationRunner, StateStore
from vmigrate.engine.dag import DagRunner, MemoryTaskStore
from vmigrate.engine.replica import MemoryReplicaStore, ReplicaRunner, sync_dag
from vmigrate.engine.safety import check_disk_counts
from vmigrate.ir import Firmware, OsFamily
from vmigrate.providers.fake.provider import FakeTarget, PatternDisk
from vmigrate.providers.xen import mapping
from vmigrate.providers.xen.api import XenApi
from vmigrate.providers.xen.source import ExportStream, XenSource

UNIT = 6 << 20
BLOCK = 64 << 10
N = 3


def content(k: int, gen: int) -> bytes:
    return PatternDisk(f"app01:{k}", UNIT, generation=gen).read(0, UNIT)


class Bytes(DiskReader):
    def __init__(self, data):
        self.data = data

    @property
    def size(self):
        return len(self.data)

    def read(self, off, n):
        return self.data[off:off + n]


class FakeXapi:
    def __init__(self, cbt=False, nbd=True):
        self.gen, self.nbd = 0, nbd
        self.vms = {"OpaqueRef:vm1": {"uuid": "u-vm1", "name_label": "app01", "power_state": "Running",
                                      "is_a_template": False, "is_control_domain": False, "is_a_snapshot": False,
                                      "VCPUs_max": "4", "memory_static_max": str(4 << 30),
                                      "HVM_boot_params": {"firmware": "uefi"}, "platform": {"cores-per-socket": "2", "secureboot": "true"},
                                      "VBDs": [f"OpaqueRef:vbd{k}" for k in (2, 0, 1)] + ["OpaqueRef:cd"],
                                      "VIFs": ["OpaqueRef:vif0"], "guest_metrics": "OpaqueRef:gm"},
                    "OpaqueRef:dom0": {"uuid": "u-d0", "name_label": "Control domain", "is_control_domain": True}}
        self.vbds = {f"OpaqueRef:vbd{k}": {"VM": "OpaqueRef:vm1", "type": "Disk", "userdevice": str(k), "VDI": f"OpaqueRef:vdi{k}",
                                           "bootable": k == 0} for k in range(N)}
        self.vbds["OpaqueRef:cd"] = {"VM": "OpaqueRef:vm1", "type": "CD", "userdevice": "3", "VDI": "OpaqueRef:NULL"}
        self.vdis = {f"OpaqueRef:vdi{k}": {"uuid": f"u-vdi{k}", "virtual_size": str(UNIT), "cbt_enabled": cbt,
                                           "sharable": False, "SR": "OpaqueRef:sr", "name_label": f"app01 {k}",
                                           "snapshot_of": "OpaqueRef:NULL", "gen": None} for k in range(N)}
        self.k = 0
        self.logins = 0
        self.destroyed: list[str] = []
        self.data_destroyed: list[str] = []
        self.cbt_enabled: list[str] = []

    def __call__(self, method, url, headers, body, timeout):
        req = json.loads(body)
        m, p = req["method"], req["params"]
        if m == "session.login_with_password":
            self.logins += 1
            if url.startswith("https://member"):
                return self.err("HOST_IS_SLAVE", "master.example.com")
            return self.ok("OpaqueRef:session", req)
        if m == "session.logout":
            return self.ok(None, req)
        assert p[0] == "OpaqueRef:session" and url.startswith("https://master")
        return self.ok(self.dispatch(m, p[1:]), req)

    def ok(self, result, req):
        return 200, {}, json.dumps({"jsonrpc": "2.0", "result": result, "id": req["id"]}).encode()

    def err(self, code, *data):
        return 200, {}, json.dumps({"jsonrpc": "2.0", "error": {"code": 1, "message": code, "data": [code, *data]}}).encode()

    def _vdi_pub(self, ref):
        return {k: v for k, v in self.vdis[ref].items() if k != "gen"}

    def dispatch(self, m, a):
        if m == "VM.get_all_records":
            return self.vms
        if m == "VM.get_by_uuid":
            return next(r for r, v in self.vms.items() if v["uuid"] == a[0])
        if m == "VM.get_record":
            if a[0] not in self.vms:
                raise KeyError(a[0])
            return self.vms[a[0]]
        if m == "VBD.get_record":
            return self.vbds[a[0]]
        if m == "VBD.get_all_records":
            return self.vbds
        if m == "VDI.get_record":
            return self._vdi_pub(a[0])
        if m == "VIF.get_record":
            return {"device": "0", "MAC": "aa:bb:cc:00:11:22", "network": "OpaqueRef:net"}
        if m == "network.get_name_label":
            return "Pool-wide network associated with eth0"
        if m == "VM_guest_metrics.get_record":
            return {"os_version": {"name": "Ubuntu 22.04.4 LTS", "distro": "ubuntu", "major": "22"}}
        if m == "VM.get_power_state":
            return self.vms["OpaqueRef:vm1"]["power_state"]
        if m in ("VM.clean_shutdown", "VM.hard_shutdown"):
            self.vms["OpaqueRef:vm1"]["power_state"] = "Halted"
            return None
        if m == "VDI.enable_cbt":
            self.vdis[a[0]]["cbt_enabled"] = True
            self.cbt_enabled.append(a[0])
            return None
        if m == "VM.snapshot":
            self.k += 1
            sref = f"OpaqueRef:snap{self.k}"
            vbds = []
            for k in range(N):
                vref = f"OpaqueRef:svdi{self.k}-{k}"
                self.vdis[vref] = {**self.vdis[f"OpaqueRef:vdi{k}"], "uuid": f"u-svdi{self.k}-{k}",
                                   "snapshot_of": f"OpaqueRef:vdi{k}", "gen": (k, self.gen)}
                bref = f"OpaqueRef:svbd{self.k}-{k}"
                self.vbds[bref] = {"VM": sref, "type": "Disk", "userdevice": str(k), "VDI": vref}
                vbds.append(bref)
            self.vms[sref] = {"uuid": f"u-snap{self.k}", "name_label": a[1], "is_a_snapshot": True, "VBDs": vbds}
            return sref
        if m == "VM.get_VBDs":
            return self.vms[a[0]]["VBDs"]
        if m == "VDI.get_nbd_info":
            if not self.nbd:
                return []
            return [{"address": "10.0.0.5", "port": 10809, "exportname": f"/{a[0]}", "cert": "", "subject": "xen1"}]
        if m == "VDI.list_changed_blocks":
            ka, ga = self.vdis[a[0]]["gen"]
            kb, gb = self.vdis[a[1]]["gen"]
            assert ka == kb and self.vdis[a[0]]["cbt_enabled"]
            old, new = content(ka, ga), content(kb, gb)
            bits = bytearray(UNIT // BLOCK // 8)
            for i in range(UNIT // BLOCK):
                if old[i * BLOCK:(i + 1) * BLOCK] != new[i * BLOCK:(i + 1) * BLOCK]:
                    bits[i // 8] |= 0x80 >> (i % 8)
            return base64.b64encode(bytes(bits)).decode()
        if m == "VDI.data_destroy":
            self.vdis[a[0]]["type"] = "cbt_metadata"
            self.data_destroyed.append(a[0])
            return None
        if m == "VDI.destroy":
            self.vdis.pop(a[0], None)
            self.destroyed.append(a[0])
            return None
        if m == "VM.destroy":
            assert self.vms[a[0]]["is_a_snapshot"]
            del self.vms[a[0]]
            self.destroyed.append(a[0])
            return None
        raise AssertionError(f"unexpected XAPI {m}")

    # data paths
    def nbd_connect(self, info):
        ref = info["exportname"].lstrip("/")
        k, g = self.vdis[ref]["gen"]
        return Bytes(content(k, g))

    def opener(self, url):
        vdi_uuid = url.split("vdi=")[1].split("&")[0]
        ref = next(r for r, v in self.vdis.items() if v["uuid"] == vdi_uuid)
        k, g = self.vdis[ref]["gen"]
        return io.BytesIO(content(k, g))


def make(tmp_path, x, url="https://master.example.com", **cfg):
    ctx = RunContext(dry_run=False, logger=logging.getLogger("t"))
    api = XenApi(url, "root", "pw", http=x)
    src = XenSource("xen", {"type": "xen", "url": url, "username": "root", **cfg}, ctx, api=api,
                    nbd_connect=x.nbd_connect, export_opener=x.opener)
    tgt = FakeTarget("tgt", {"staging_dir": str(tmp_path / "tgt"), "persist": True}, ctx)
    e = EngineConfig(state_dir=tmp_path / "s", report_dir=tmp_path / "r", chunk_size=1 << 20, checkpoint_every=2 << 20,
                     verify_samples=4, verify_sample_bytes=64 << 10)
    return ctx, src, tgt, e


def test_mapping_order_firmware_and_bitmap():
    x = FakeXapi()
    vm = x.vms["OpaqueRef:vm1"]
    vbds = {b: x.vbds[b] for b in vm["VBDs"]}
    vdis = {f"OpaqueRef:vdi{k}": x._vdi_pub(f"OpaqueRef:vdi{k}") for k in range(N)}
    spec = mapping.to_vmspec(vm, vbds, vdis, [{"device": "0", "MAC": "aa:bb", "network": "n"}], {"n": "eth0"},
                             {"os_version": {"name": "Ubuntu 22.04"}})
    assert [d.id for d in spec.ordered_disks()] == ["u-vdi0", "u-vdi1", "u-vdi2"] and spec.boot_disk().id == "u-vdi0"
    assert spec.disks[0].controller == "xen-blkfront" and spec.nics[0].model == "xen-netfront"
    assert spec.firmware == Firmware.UEFI and spec.secure_boot and spec.cpu.sockets == 2 and spec.cpu.total == 4
    assert spec.guest_os.family == OsFamily.LINUX and spec.guest_os.distro == "ubuntu"
    assert check_disk_counts(spec, mapping.crosscheck(vbds, 3))
    with pytest.raises(DiskCountMismatch):
        check_disk_counts(spec, mapping.crosscheck(vbds, 2))
    bm = base64.b64encode(bytes([0b10100000, 0, 0b00000001])).decode()
    r = [(x.offset // BLOCK, x.length // BLOCK) for x in mapping.bitmap_to_ranges(bm, 24 * BLOCK)]
    assert r == [(0, 1), (2, 1), (23, 1)]


def test_login_follows_pool_master():
    x = FakeXapi()
    api = XenApi("https://member1.example.com", "root", "pw", http=x)
    api.login()
    assert api.url == "https://master.example.com" and x.logins == 2
    assert api.call("VM.get_power_state", "x") == "Running"


def test_migration_over_nbd_and_cleanup(tmp_path):
    x = FakeXapi()
    ctx, src, tgt, e = make(tmp_path, x)
    assert [v.name for v in src.list_vms()] == ["app01"] and src.check_connection() == []
    job = MigrationConfig(name="m", source="xen", target="tgt", vm="app01", app_consistent=False,
                          osmorph={"allow_manual": True})
    st = MigrationRunner(job, e, src, tgt, ctx, StateStore(e.state_dir)).run()
    assert st.phase.value == "done" and st.morph["status"] == "manual_required"
    for k in range(N):
        assert tgt._final(tgt.disks[f"app01-disk{k:02d}"]).read_bytes() == content(k, 0)
    assert "OpaqueRef:snap1" in x.destroyed and not any(r.startswith("OpaqueRef:svdi") for r in x.vdis)


def test_export_stream_fallback_and_refusals(tmp_path):
    x = FakeXapi(nbd=False)
    ctx, src, tgt, e = make(tmp_path, x)
    job = MigrationConfig(name="m", source="xen", target="tgt", vm="app01", app_consistent=False,
                          osmorph={"allow_manual": True})
    assert MigrationRunner(job, e, src, tgt, ctx, StateStore(e.state_dir)).run().phase.value == "done"
    for k in range(N):
        assert tgt._final(tgt.disks[f"app01-disk{k:02d}"]).read_bytes() == content(k, 0)
    with pytest.raises(SafetyGateError, match="quiesced"):
        src.create_snapshot(src.find_vm("app01"), True)
    s = ExportStream(lambda: "u", UNIT, None, opener=lambda u: io.BytesIO(content(0, 0)))
    assert s.read(BLOCK, 10) == content(0, 0)[BLOCK:BLOCK + 10] and s.read(0, 5) == content(0, 0)[:5]
    assert s.restarts == 1


def test_cbt_replica_keeps_only_metadata(tmp_path):
    x, store = FakeXapi(), MemoryReplicaStore()

    def sync():
        ctx, src, tgt, e = make(tmp_path, x, enable_cbt=True)
        rr = ReplicaRunner(MigrationConfig(name="rep", source="xen", target="tgt", vm="app01", app_consistent=False),
                           e, src, tgt, ctx, store)
        ts = MemoryTaskStore(sync_dag(False))
        assert DagRunner(ts, rr.sync_handlers()).run().value == "completed", [t.error for t in ts.load_tasks() if t.error]
        return rr, tgt

    sync()
    assert sorted(x.cbt_enabled) == [f"OpaqueRef:vdi{k}" for k in range(N)]
    assert sorted(x.data_destroyed) == [f"OpaqueRef:svdi1-{k}" for k in range(N)] and "OpaqueRef:snap1" in x.vms
    x.gen = 1
    rr, tgt = sync()
    last = rr.state.syncs[-1]
    assert last["modes"] == ["cbt"] and last["changed_bytes"] < N * UNIT
    assert "OpaqueRef:snap1" not in x.vms and "OpaqueRef:snap2" in x.vms     # previous metadata snapshot removed
    for k in range(N):
        assert tgt._final(tgt.disks[f"rep-r{k:02d}"]).read_bytes() == content(k, 1)


def test_cutover_shutdown(tmp_path):
    x = FakeXapi()
    _, src, _, _ = make(tmp_path, x)
    vm = src.find_vm("u-vm1")
    assert src.shutdown_vm(vm) == "app01 shut down" and src.shutdown_vm(vm) == "app01 already halted"
