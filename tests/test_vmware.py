"""Purpose: VMware source tests without a vCenter: pure mapping + safety refusals, the NBD client
against an in-test NBD server, the HTTPS flat-file reader, nbdkit-vddk argv hygiene, and full
engine runs (migration + CBT replica) through VmwareSource with a fake vSphere client.

Usage: pytest tests/test_vmware.py
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import tempfile
import threading
import urllib.parse

import pytest

from vmigrate.config.loader import EngineConfig, MigrationConfig
from vmigrate.core.context import RunContext
from vmigrate.core.errors import ChangeTrackingUnavailable, DiskCountMismatch, ManualMorphRequired, SafetyGateError
from vmigrate.core.nbd import NbdError, NbdReader
from vmigrate.engine import MigrationRunner, StateStore
from vmigrate.engine.dag import DagRunner, MemoryTaskStore
from vmigrate.engine.replica import MemoryReplicaStore, ReplicaRunner, sync_dag
from vmigrate.engine.safety import check_disk_counts
from vmigrate.ir import DiskBus, OsFamily
from vmigrate.providers.fake.provider import FakeTarget, PatternDisk
from vmigrate.providers.vmware import mapping
from vmigrate.providers.vmware.source import VmwareSource
from vmigrate.providers.vmware.transports import HttpFlatReader, vddk_argv, vddk_problems

SIZE = 2 << 20
UNITS = [0, 1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 13]   # scsi0:7 is the controller itself


def vm_info(ndisks: int = 13, *, layout: int | None = None, **disk_over) -> dict:
    disks = []
    for i in range(ndisks):
        d = {"key": 2000 + i, "controller_key": 1000, "unit_number": UNITS[i], "capacity_bytes": SIZE,
             "file_name": f"[ds1] RIS-Sainath/RIS-Sainath_{i}.vmdk", "datastore": "ds1",
             "backing_type": "FlatVer2BackingInfo", "disk_mode": "persistent", "rdm_mode": None,
             "parent_file": None, "uuid": f"6000C29{i:09d}", "change_id": None}
        if i == ndisks - 1:
            d.update(disk_over)
        disks.append(d)
    return {"moref": "vm-42", "name": "RIS-Sainath", "uuid": "4201", "num_cpu": 4, "cores_per_socket": 2,
            "memory_mb": 8192, "firmware": "bios", "secure_boot": False, "guest_id": "rhel8_64Guest",
            "guest_full_name": "Red Hat Enterprise Linux 8 (64-bit)", "cbt_enabled": True, "boot_disk_keys": [2000],
            "controllers": [{"key": 1000, "type": "ParaVirtualSCSIController", "bus_number": 0},
                            {"key": 15000, "type": "VirtualAHCIController", "bus_number": 0}],
            "disks": disks, "nics": [{"key": 4000, "type": "VirtualVmxnet3", "mac": "00:50:56:aa", "network": "VM Network"}],
            "layout_disks": [{"key": d["key"], "files": [1]} for d in disks][: layout if layout is not None else ndisks],
            "datacenter": "DC1"}


# ---------------------------------------------------------------------- mapping

def test_mapping_13_disks_pvscsi_and_crosscheck():
    info = vm_info()
    spec = mapping.to_vmspec(info)
    assert len(spec.disks) == 13 and spec.boot_disk().label == "scsi0:0"
    assert [d.label for d in spec.ordered_disks()][-3:] == ["scsi0:11", "scsi0:12", "scsi0:13"]  # beyond 10
    assert {d.controller for d in spec.disks} == {"pvscsi"} and spec.disks[0].bus == DiskBus.SCSI
    assert spec.cpu.sockets == 2 and spec.cpu.cores == 2 and spec.memory_bytes == 8 << 30
    assert spec.guest_os.family == OsFamily.LINUX and spec.guest_os.distro == "rhel"
    assert spec.nics[0].model == "vmxnet3" and spec.nics[0].network == "VM Network"
    assert check_disk_counts(spec, mapping.crosscheck(info))["vsphere.layoutEx.disk"] == 13
    with pytest.raises(DiskCountMismatch):            # an enumeration that lost disks fails loud
        check_disk_counts(spec, mapping.crosscheck(vm_info(layout=10)))


@pytest.mark.parametrize("over,msg", [
    ({"disk_mode": "independent_persistent"}, "independent"),
    ({"rdm_mode": "physicalMode", "backing_type": "RawDiskMappingVer1BackingInfo"}, "physical-mode RDM"),
    ({"controller_key": 9999}, "unknown controller"),
])
def test_mapping_refusals(over, msg):
    with pytest.raises(mapping.VmwareMappingError, match=msg):
        mapping.to_vmspec(vm_info(**over))


def test_guest_os_and_flat_rules():
    assert mapping.guest_os("windows2019srv_64Guest", "").family == OsFamily.WINDOWS
    assert mapping.guest_os("ubuntu64Guest", "").distro == "ubuntu"
    assert mapping.guest_os("freebsd13_64Guest", "FreeBSD").family == OsFamily.UNKNOWN
    assert mapping.flat_base_path("[ds 1] a b/c.vmdk") == ("ds 1", "a b/c-flat.vmdk")
    assert mapping.flat_readable({"backing_type": "FlatVer2BackingInfo", "parent_file": None})[0]
    ok, why = mapping.flat_readable({"backing_type": "FlatVer2BackingInfo", "parent_file": "[ds1] x-000001.vmdk"})
    assert not ok and "snapshots before ours" in why
    assert not mapping.flat_readable({"backing_type": "SeSparseBackingInfo"})[0]


def test_changed_ranges_paging():
    calls = []

    def q(start):
        calls.append(start)
        return (start, 1 << 20, [(start + 4096, 8192)])   # vSphere answers in 1 MiB windows
    r = mapping.changed_ranges(q, 3 << 20)
    assert calls == [0, 1 << 20, 2 << 20] and len(r) == 3 and r[2].offset == (2 << 20) + 4096


# ---------------------------------------------------------------------- NBD

def _nbd_server(sock_path: str, data: bytes, *, fail_at: int | None = None) -> threading.Thread:
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(1)

    def rx(c, n):
        b = b""
        while len(b) < n:
            k = c.recv(n - len(b))
            if not k:
                raise EOFError
            b += k
        return b

    def run():
        c, _ = srv.accept()
        try:
            c.sendall(b"NBDMAGIC" + struct.pack(">QH", 0x49484156454F5054, 3))
            assert struct.unpack(">I", rx(c, 4))[0] & 1
            _, opt, ln = struct.unpack(">QII", rx(c, 16))
            rx(c, ln)
            assert opt == 7
            info = struct.pack(">HQH", 0, len(data), 1)
            c.sendall(struct.pack(">QIII", 0x3E889045565A9, 7, 3, len(info)) + info)
            c.sendall(struct.pack(">QIII", 0x3E889045565A9, 7, 1, 0))
            while True:
                magic, _f, typ, h, off, ln = struct.unpack(">IHHQQI", rx(c, 28))
                if typ == 2:
                    return
                if fail_at is not None and off >= fail_at:
                    c.sendall(struct.pack(">IIQ", 0x67446698, 5, h))
                    continue
                c.sendall(struct.pack(">IIQ", 0x67446698, 0, h) + data[off:off + ln])
        except EOFError:
            pass
        finally:
            c.close()
            srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_nbd_client_reads_and_splits():
    data = PatternDisk("nbd", 3 << 20).read(0, 3 << 20)
    path = os.path.join(tempfile.mkdtemp(), "s")
    _nbd_server(path, data)
    with NbdReader.connect_unix(path, max_request=256 << 10) as r:
        assert r.size == len(data)
        assert r.read(100, (1 << 20) + 7) == data[100:100 + (1 << 20) + 7]   # split across requests
        assert r.read(len(data) - 10, 100) == data[-10:]                    # clipped at EOF


def test_nbd_client_surfaces_read_errors():
    path = os.path.join(tempfile.mkdtemp(), "s")
    _nbd_server(path, bytes(1 << 20), fail_at=4096)
    r = NbdReader.connect_unix(path)
    assert r.read(0, 4096) == bytes(4096)
    with pytest.raises(NbdError, match="read error 5"):
        r.read(4096, 10)


# ---------------------------------------------------------------------- transports

class Resp:
    def __init__(self, code, content):
        self.status_code, self.content, self.headers = code, content, {}


def test_http_flat_reader_range_only():
    data = bytes(range(256)) * 64
    r = HttpFlatReader(lambda url, headers: Resp(206, data[int(headers["Range"][6:].split("-")[0]):
                                                          int(headers["Range"].split("-")[1]) + 1]), "u", len(data))
    assert r.read(10, 300) == data[10:310]
    whole = HttpFlatReader(lambda url, headers: Resp(200, data), "u", len(data))
    with pytest.raises(Exception, match="ignored the Range"):
        whole.read(0, 10)


def test_vddk_argv_keeps_password_off_the_command_line():
    argv = vddk_argv(nbdkit="nbdkit", sock="/s", libdir="/vddk", server="vc", user="u", password_file="/tmp/pw",
                     thumbprint="AA:BB", vm_moref="vm-42", snapshot_moref="snapshot-7",
                     file_name="[ds1] a/a.vmdk", transports="nbdssl:nbd")
    assert "password=+/tmp/pw" in argv and "--readonly" in argv and "snapshot=snapshot-7" in argv
    assert not any("secret" in a for a in argv)
    assert any("nbdkit not found" in p or "VDDK library" in p for p in vddk_problems("no-such-nbdkit", "/nope"))


# ---------------------------------------------------------------------- engine runs

class FakeVsphere:
    """Just enough vSphere: 13 pvscsi disks, snapshots that freeze a 'generation', CBT."""

    def __init__(self):
        self.gen = 0
        self.snaps: dict[str, dict] = {}
        self.removed: list[str] = []
        self.cbt_reset = False
        self.prior_snapshot = False
        self.n = 0

    def about(self):
        return {"name": "fake vCenter"}

    def list_vms(self):
        return [{"moref": "vm-42", "name": "RIS-Sainath", "power": "poweredOn"}]

    def vm_info(self, moref):
        return vm_info()

    def create_snapshot(self, moref, name, *, quiesce):
        self.n += 1
        s = f"snapshot-{self.n}"
        self.snaps[s] = {"name": name, "gen": self.gen}
        return s

    def snapshot_disks(self, moref, s):
        out = []
        for d in vm_info()["disks"]:
            out.append({**d, "change_id": f"gen{self.snaps[s]['gen']}",
                        "parent_file": "[ds1] RIS-Sainath/RIS-Sainath_0-000001.vmdk" if self.prior_snapshot else None})
        return out

    def remove_snapshot(self, moref, s):
        assert self.snaps[s]["name"].startswith("vmigrate-")
        self.removed.append(s)
        self.snaps.pop(s)

    def query_changed(self, moref, s, key, start, change_id):
        i = key - 2000
        if change_id == "*":
            return 0, SIZE, [(0, SIZE)]
        if self.cbt_reset:
            raise ChangeTrackingUnavailable("CBT was reset")
        base = int(change_id.removeprefix("gen"))
        blocks = PatternDisk(f"RIS-Sainath:{i}", SIZE).changed_blocks(base, self.snaps[s]["gen"])
        return 0, SIZE, [(b * (64 << 10), 64 << 10) for b in blocks]

    def session_cookie(self):
        return "vmware_soap_session=x"

    def get(self, url, headers):
        path = urllib.parse.urlparse(url).path
        i = int(path.rsplit("_", 1)[1].split("-flat")[0])
        live = [v for v in self.snaps.values()]
        gen = live[-1]["gen"]                          # base flat file == state at our (only) snapshot
        a, b = (int(x) for x in headers["Range"][6:].split("-"))
        return Resp(206, PatternDisk(f"RIS-Sainath:{i}", SIZE, generation=gen).read(a, b - a + 1))

    def close(self):
        pass


class HttpTestSource(VmwareSource):
    def _http_get(self):
        return self._client.get


def _setup(tmp_path, fv, execute=True):
    ctx = RunContext(dry_run=not execute, logger=logging.getLogger("t"))
    src = HttpTestSource("vc", {"type": "vmware", "host": "vc.example", "username": "u", "password": "p",
                                  "transport": "http"}, ctx, client=fv)
    tgt = FakeTarget("tgt", {"staging_dir": str(tmp_path / "tgt"), "persist": True}, ctx)
    e = EngineConfig(state_dir=tmp_path / "s", report_dir=tmp_path / "r", chunk_size=1 << 20, checkpoint_every=2 << 20,
                     verify_samples=4, verify_sample_bytes=64 << 10)
    return ctx, src, tgt, e


def test_migration_from_vmware_stops_at_required_virtio_osmorph(tmp_path):
    fv = FakeVsphere()
    ctx, src, tgt, e = _setup(tmp_path, fv)
    job = MigrationConfig(name="m", source="vc", target="tgt", vm="RIS-Sainath")
    r = MigrationRunner(job, e, src, tgt, ctx, StateStore(e.state_dir))
    with pytest.raises(ManualMorphRequired, match="pvscsi"):   # no guestfish here -> refuse to create a VM
        r.run()
    assert not tgt.vms and all(r.state.disks[d].committed for d in r.state.disks)  # data is staged, nothing booted
    # operator injects virtio manually, then allows the run to continue
    job2 = MigrationConfig(name="m", source="vc", target="tgt", vm="RIS-Sainath", osmorph={"allow_manual": True})
    st = MigrationRunner(job2, e, src, tgt, ctx, StateStore(e.state_dir)).run()
    assert st.phase.value == "done" and len(tgt.vms["RIS-Sainath"]["disks"]) == 13
    for i in range(13):
        assert tgt._final(tgt.disks[f"RIS-Sainath-disk{i:02d}"]).read_bytes() == PatternDisk(f"RIS-Sainath:{i}", SIZE).read(0, SIZE)
    assert fv.removed and not fv.snaps                           # our snapshot removed, nothing else touched


def test_http_transport_refuses_disks_with_prior_snapshots(tmp_path):
    fv = FakeVsphere()
    fv.prior_snapshot = True
    ctx, src, tgt, e = _setup(tmp_path, fv)
    r = MigrationRunner(MigrationConfig(name="m", source="vc", target="tgt", vm="RIS-Sainath"), e, src, tgt, ctx,
                        StateStore(e.state_dir))
    with pytest.raises(SafetyGateError, match="snapshots before ours"):
        r.run()


def test_vmware_cbt_replica_and_reset_fallback(tmp_path):
    fv = FakeVsphere()
    store = MemoryReplicaStore()

    def sync():
        ctx, src, tgt, e = _setup(tmp_path, fv)
        rr = ReplicaRunner(MigrationConfig(name="rep", source="vc", target="tgt", vm="RIS-Sainath"), e, src, tgt, ctx,
                           store)
        ts = MemoryTaskStore(sync_dag(False))
        assert DagRunner(ts, rr.sync_handlers()).run().value == "completed", [t.error for t in ts.load_tasks() if t.error]
        return rr, tgt

    rr, tgt = sync()
    assert rr.state.syncs[-1]["modes"] == ["full"] and not fv.snaps     # vmware keeps no base snapshot
    fv.gen = 1
    rr, tgt = sync()
    last = rr.state.syncs[-1]
    assert last["modes"] == ["cbt"] and last["bytes_read"] < 13 * SIZE // 3
    fv.gen, fv.cbt_reset = 2, True
    rr, tgt = sync()
    assert rr.state.syncs[-1]["modes"] == ["compare"]                    # CBT reset -> correct, slower
    assert rr.state.current is None
    for i in range(13):
        assert tgt._final(tgt.disks[f"rep-r{i:02d}"]).read_bytes() == PatternDisk(f"RIS-Sainath:{i}", SIZE, generation=2).read(0, SIZE)
