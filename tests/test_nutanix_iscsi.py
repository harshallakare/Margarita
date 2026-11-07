"""Purpose: the Nutanix iSCSI transport (Prism Central v4 recovery point -> volume group -> iSCSI),
without a cluster: v4 request bodies + task handling + guards, iSCSI target/device parsing and the
LUN -> disk mapping (both Nutanix layouts), transport auto-selection, and a full 13-disk migration
through NutanixSource with fake v4 + fake iSCSI devices, including cleanup order and refusals.

Usage: pytest tests/test_nutanix_iscsi.py
"""

from __future__ import annotations

import logging
import os
from types import SimpleNamespace

import pytest

from vmigrate.config.loader import EngineConfig, MigrationConfig
from vmigrate.core.context import RunContext
from vmigrate.core.errors import ProviderError, SafetyGateError
from vmigrate.engine import MigrationRunner, StateStore
from vmigrate.providers.fake.provider import FakeTarget, PatternDisk
from vmigrate.providers.nutanix import iscsi, v4
from vmigrate.providers.nutanix.source import NutanixSource

from .fakes_platform import FakePrism

SIZE = 3 << 20


# ---------------------------------------------------------------------- v4 API module

class Resp:
    def __init__(self, status, data=None, headers=None):
        self.status_code, self._d, self.headers = status, data, headers or {}
        self.content = b"x" if data is not None else b""
        self.text = str(data)

    def json(self):
        return self._d


class Session:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def request(self, method, url, json=None, headers=None, timeout=None):
        path = url.split("9440", 1)[1]
        self.calls.append((method, path, json, headers))
        r = self.routes.get((method, path))
        return r(json) if callable(r) else (r or Resp(404, {"error": "no route"}))


def test_v4_bodies_and_guards():
    b = v4.recovery_point_body("vm-1", "vmigrate-r", app_consistent=True)
    assert b["recoveryPointType"] == "APPLICATION_CONSISTENT" and b["vmRecoveryPoints"] == [{"vmExtId": "vm-1"}]
    assert v4.vg_disk_body("rp", "drp", 3)["diskDataSourceReference"]["entityType"] == "DISK_RECOVERY_POINT"
    for f in (lambda: v4.recovery_point_body("vm", "prod-rp", app_consistent=False),
              lambda: v4.volume_group_body("prod-vg", "cl")):
        with pytest.raises(SafetyGateError):
            f()


def test_v4_tasks_headers_and_delete_guard():
    task = {"data": {"status": "SUCCEEDED", "entitiesAffected": [
        {"rel": "dataprotection:config:recovery-point", "extId": "rp-9"}]}}
    s = Session({("POST", v4.RECOVERY_POINTS): Resp(202, {"data": {"extId": "t1"}}),
                 ("GET", f"{v4.TASKS}/t1"): Resp(200, task),
                 ("GET", f"{v4.RECOVERY_POINTS}/rp-9"): Resp(200, {"data": {"name": "prod-backup"}}, {"ETag": "e1"}),
                 ("GET", v4.CLUSTERS): Resp(404, {})})
    api = v4.PrismV4(s, "https://pc:9440", sleep=lambda x: None)
    assert api.create_recovery_point("vm-1", "vmigrate-r", app_consistent=False) == "rp-9"
    post = next(c for c in s.calls if c[0] == "POST")
    assert post[3]["NTNX-Request-Id"]                          # idempotency id on every write
    with pytest.raises(SafetyGateError, match="not created by vmigrate"):
        api.delete_recovery_point("rp-9")                       # never deletes someone else's RP
    with pytest.raises(ProviderError, match="Prism Central >= 7.3"):
        api.clusters()                                          # pre-v4 Prism Central: clear message


def test_v4_missing_field_fails_loudly_with_payload():
    s = Session({("GET", f"{v4.RECOVERY_POINTS}/rp"): Resp(200, {"data": {"unexpected": 1}})})
    with pytest.raises(ProviderError, match="lacks 'data.vmRecoveryPoints'.*unexpected"):
        v4.PrismV4(s, "https://pc:9440").recovery_point_disks("rp")


# ---------------------------------------------------------------------- iSCSI parsing + mapping

def test_sendtargets_and_by_path_parsing():
    out = ("10.0.0.50:3260,1 iqn.2010-06.com.nutanix:vmigrate-abc-1234-tgt0\n"
           "10.0.0.50:3260,1 iqn.2010-06.com.nutanix:vmigrate-abc-1234-tgt1\n"
           "10.0.0.50:3260,1 iqn.2010-06.com.nutanix:prod-db-vg-tgt0\n"
           "10.9.9.9:3260,1 iqn.2010-06.com.nutanix:vmigrate-abc-other\n")
    t = iscsi.parse_sendtargets(out, "10.0.0.50:3260", "vmigrate-abc")
    assert t == ["iqn.2010-06.com.nutanix:vmigrate-abc-1234-tgt0", "iqn.2010-06.com.nutanix:vmigrate-abc-1234-tgt1"]
    listing = [f"ip-10.0.0.50:3260-iscsi-{t[0]}-lun-0", f"ip-10.0.0.50:3260-iscsi-{t[1]}-lun-0",
               f"ip-10.0.0.50:3260-iscsi-{t[0]}-lun-0-part1", "ip-10.0.0.50:3260-iscsi-iqn.x:prod-lun-0"]
    assert [d[0] for d in iscsi.by_path_devices(listing, "10.0.0.50:3260", t)] == t


def test_map_devices_both_layouts_and_refusals():
    disks = [{"index": i, "size": 100 + i} for i in range(3)]
    size = {f"/d/{k}": 100 + int(k[-1]) for k in ("l0", "l1", "l2", "t0", "t1", "t2")}.__getitem__
    one = [("iqn:vg", i, f"/d/l{i}") for i in range(3)]
    assert iscsi.map_devices(disks, one, size) == {0: "/d/l0", 1: "/d/l1", 2: "/d/l2"}
    virt = [(f"iqn:vg-tgt{i}", 0, f"/d/t{i}") for i in range(3)]
    assert iscsi.map_devices(disks, virt, size) == {0: "/d/t0", 1: "/d/t1", 2: "/d/t2"}
    with pytest.raises(SafetyGateError, match="2 LUNs but the volume group has 3"):
        iscsi.map_devices(disks, one[:2], size)
    with pytest.raises(SafetyGateError, match="ambiguous"):
        iscsi.map_devices(disks, [("iqn:vg", 0, "/d/l1"), ("iqn:vg", 1, "/d/l0"), ("iqn:vg", 2, "/d/l2")], size)


def test_iscsi_session_logs_out_only_its_targets():
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        out = ""
        if "discovery" in argv:
            out = "10.0.0.50:3260,1 iqn.x:vmigrate-r-tgt0\n10.0.0.50:3260,1 iqn.x:prod-tgt0\n"
        if argv[1:3] == ["-m", "session"]:
            out = "tcp: [3] 10.0.0.50:3260,1 iqn.x:vmigrate-r-tgt0 (non-flash)\ntcp: [4] 10.0.0.50:3260,1 iqn.x:prod (non-flash)\n"
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    s = iscsi.IscsiSession("10.0.0.50", run=run, listdir=lambda p: ["ip-10.0.0.50:3260-iscsi-iqn.x:vmigrate-r-tgt0-lun-0"],
                           sleep=lambda x: None)
    devs = s.login("vmigrate-r", expected=1)
    assert devs == [("iqn.x:vmigrate-r-tgt0", 0, "/dev/disk/by-path/ip-10.0.0.50:3260-iscsi-iqn.x:vmigrate-r-tgt0-lun-0")]
    s.logout()
    s.logout_matching("vmigrate-r")
    touched = {c[c.index("-T") + 1] for c in calls if "-T" in c}
    assert touched == {"iqn.x:vmigrate-r-tgt0"}                 # never the production target


# ---------------------------------------------------------------------- full migration through NutanixSource

class FakeV4:
    def __init__(self, n=13, dsip="10.0.0.50", reachable=True, drop_disk=False):
        self.n, self.dsip, self.reachable, self.drop_disk = n, dsip, reachable, drop_disk
        self.log: list[str] = []
        self.vg_map: dict[int, str] = {}

    def clusters(self):
        if not self.reachable:
            raise ProviderError("Prism v4 API not available (needs Prism Central >= 7.3)")
        return [{"extId": "cl"}]

    def data_services_ip(self, cluster):
        return self.dsip if cluster == "cl" else None

    def create_recovery_point(self, vm, name, app_consistent):
        self.log.append(f"rp:{name}:{'app' if app_consistent else 'crash'}")
        return "rp-1"

    def recovery_point_disks(self, rp):
        n = self.n - 1 if self.drop_disk else self.n
        return {f"vd-{i:02d}": f"drp-{i:02d}" for i in range(n)}

    def create_volume_group(self, name, cluster):
        self.log.append(f"vg:{name}")
        return "vg-1"

    def add_disk_from_recovery_point(self, vg, rp, drp, index):
        self.vg_map[index] = drp

    def vg_disks(self, vg):
        return [{"ext": f"vgd-{i}", "index": i, "size": SIZE} for i in sorted(self.vg_map)]

    def attach_iscsi_client(self, vg, iqn):
        self.log.append(f"attach:{iqn}")

    def detach_iscsi_client(self, vg, iqn):
        self.log.append("detach")

    def delete_volume_group(self, vg):
        self.log.append("delete-vg")

    def delete_recovery_point(self, rp):
        self.log.append("delete-rp")


def iscsi_factory(fv4, devdir):
    class FakeIscsi:
        def __init__(self, portal):
            self.portal = portal

        def initiator_name(self):
            return "iqn.1994-05.com.redhat:worker1"

        def login(self, vg_name, expected):
            fv4.log.append("login")
            devdir.mkdir(exist_ok=True)
            out = []
            for idx, drp in sorted(fv4.vg_map.items()):
                p = devdir / f"{vg_name}-lun-{idx}"
                p.write_bytes(PatternDisk(drp.replace("drp-", "vd-"), SIZE).read(0, SIZE))
                out.append((f"iqn.x:{vg_name}", idx, str(p)))
            return out

        def logout(self):
            fv4.log.append("logout")

        def logout_matching(self, name):
            pass
    return FakeIscsi


def make(tmp_path, fv4, transport="auto", **cfg):
    ctx = RunContext(dry_run=False, logger=logging.getLogger("t"))
    src = NutanixSource("ahv", {"prism_element": "https://pe:9440", "prism_central": "https://pc:9440",
                                "username": "admin", "transport": transport, **cfg}, ctx,
                        client=FakePrism.with_vm(tmp_path, disks=13, size=SIZE), v4=fv4,
                        iscsi_factory=iscsi_factory(fv4, tmp_path / "dev"), size_of=os.path.getsize)
    tgt = FakeTarget("tgt", {"staging_dir": str(tmp_path / "tgt"), "persist": True}, ctx)
    e = EngineConfig(state_dir=tmp_path / "s", report_dir=tmp_path / "r", chunk_size=1 << 20,
                     checkpoint_every=2 << 20, verify_samples=4, verify_sample_bytes=64 << 10)
    return ctx, src, tgt, e


def test_iscsi_migration_13_disks_no_nfs_no_pd(tmp_path):
    fv4 = FakeV4()
    ctx, src, tgt, e = make(tmp_path, fv4)
    assert src.check_connection() == []                         # no protection_domain needed on this path
    st = MigrationRunner(MigrationConfig(name="m", source="ahv", target="tgt", vm="RIS-Sainath"), e, src, tgt, ctx,
                         StateStore(e.state_dir)).run()
    assert st.phase.value == "done" and len(tgt.vms["RIS-Sainath"]["disks"]) == 13
    for i in range(13):
        got = tgt._final(tgt.disks[f"RIS-Sainath-disk{i:02d}"]).read_bytes()
        assert got == PatternDisk(f"vd-{i:02d}", SIZE).read(0, SIZE)
    assert fv4.vg_map == {i: f"drp-{i:02d}" for i in range(13)}   # LUN index == source disk order
    assert fv4.log[0].endswith(":app") and fv4.log[-4:] == ["logout", "detach", "delete-vg", "delete-rp"]
    src.close()


def test_recovery_point_missing_a_disk_is_refused_and_cleaned_up(tmp_path):
    fv4 = FakeV4(drop_disk=True)
    ctx, src, tgt, e = make(tmp_path, fv4)
    with pytest.raises(SafetyGateError, match="no unique copy of disk scsi.12"):
        MigrationRunner(MigrationConfig(name="m", source="ahv", target="tgt", vm="RIS-Sainath"), e, src, tgt, ctx,
                        StateStore(e.state_dir)).run()
    assert "delete-rp" in fv4.log and not tgt.disks             # partial objects removed; nothing created


def test_transport_auto_selection(tmp_path):
    ctx, src, _, _ = make(tmp_path, FakeV4())
    assert src.transport_for("cl")[0] == "iscsi"
    ctx, src, _, _ = make(tmp_path / "b", FakeV4(dsip=None))
    mode, why = src.transport_for("cl")
    assert mode == "nfs" and "Data Services IP" in why
    ctx, src, _, _ = make(tmp_path / "c", FakeV4(reachable=False))
    assert src.transport_for("cl")[0] == "nfs"
    assert any("protection_domain is required" in p for p in src.check_connection())  # NFS fallback needs a PD
    ctx, src, _, _ = make(tmp_path / "d", FakeV4(reachable=False), transport="iscsi")
    with pytest.raises(ProviderError, match="transport iscsi unavailable"):
        src.transport_for("cl")
    assert any("transport iscsi" in p for p in src.check_connection())
