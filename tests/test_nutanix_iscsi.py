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

    def recovery_point_layout(self, rp):
        return {"vm_recovery_point": f"vmrp-{rp}", "disks": self.recovery_point_disks(rp)}

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


# ---------------------------------------------------------------------- CRT (changed regions)
CUR = {"recovery_point": "rp-2", "vm_recovery_point": "vmrp-2", "disk_recovery_point": "drp-2"}
REF = {"recovery_point": "rp-1", "vm_recovery_point": "vmrp-1", "disk_recovery_point": "drp-1"}
COMPUTE = (f"/api/dataprotection/v4.0/content/recovery-points/rp-2/vm-recovery-points/vmrp-2/"
           "disk-recovery-points/drp-2/$actions/compute-changed-regions")
DISCOVER = "/api/dataprotection/v4.0/config/recovery-points/rp-2/$actions/discover-cluster"


def test_crt_discover_paging_jwt_refresh_and_zeroed_regions():
    pages = {0: ([{"offset": 0, "length": 4096, "regionType": "REGULAR"}], "1048576"),
             1048576: ([{"offset": 1048576, "length": 65536, "regionType": "ZEROED"}], "0")}
    state = {"jwt": 0, "expired_once": False}

    def discover(body):
        state["jwt"] += 1
        return Resp(200, {"data": {"clusterIp": {"ipv4": {"value": "10.0.0.5"}}, "jwtToken": f"jwt{state['jwt']}"}})

    def compute(body):
        if body["offset"] == 1048576 and not state["expired_once"]:
            state["expired_once"] = True
            return Resp(401, {"error": "expired"})
        regs, nxt = pages[body["offset"]]
        return Resp(200, {"data": regs, "metadata": {"extraInfo": [{"name": "nextOffset", "value": nxt},
                                                                     {"name": "fileSize", "value": "4194304"}]}})
    s = Session({("POST", DISCOVER): discover, ("POST", COMPUTE): compute})
    api = v4.PrismV4(s, "https://pc:9440", sleep=lambda x: None)
    regs = api.changed_regions(CUR, REF, 4 << 20)
    assert regs == [(0, 4096, "REGULAR"), (1048576, 65536, "ZEROED")]
    d = next(c for c in s.calls if c[1] == DISCOVER)
    assert d[2]["operation"] == "COMPUTE_CHANGED_REGIONS"
    assert d[2]["spec"]["$objectType"] == "dataprotection.v4.content.ComputeChangedRegionsClusterDiscoverSpec"
    assert d[2]["spec"]["diskRecoveryPoint"] == {"$objectType": "dataprotection.v4.content.VmDiskRecoveryPointReference",
                                                 "recoveryPointExtId": "rp-2", "vmRecoveryPointExtId": "vmrp-2",
                                                 "diskRecoveryPointExtId": "drp-2"}
    assert d[2]["spec"]["referenceDiskRecoveryPoint"]["diskRecoveryPointExtId"] == "drp-1"
    c0 = [c for c in s.calls if c[1] == COMPUTE]
    assert c0[0][2]["referenceRecoveryPointExtId"] == "rp-1" and c0[0][2]["referenceDiskRecoveryPointExtId"] == "drp-1"
    assert c0[0][3]["Cookie"] == "NTNX_IGW_SESSION=jwt1" and c0[-1][3]["Cookie"] == "NTNX_IGW_SESSION=jwt2"
    assert state["jwt"] == 2                                     # rediscovered once after the 401


def test_set_expiration_guard_and_body():
    s = Session({("GET", f"{v4.RECOVERY_POINTS}/rp-1"): Resp(200, {"data": {"name": "vmigrate-x"}}, {"ETag": "e7"}),
                 ("POST", f"{v4.RECOVERY_POINTS}/rp-1/$actions/set-expiration-time"): Resp(200, {}),
                 ("GET", f"{v4.RECOVERY_POINTS}/rp-9"): Resp(200, {"data": {"name": "prod"}}, {"ETag": "e"})})
    api = v4.PrismV4(s, "https://pc:9440")
    api.set_expiration("rp-1", 48)
    post = next(c for c in s.calls if c[0] == "POST")
    assert post[2]["expirationTime"].endswith("Z") and post[3]["If-Match"] == "e7"
    with pytest.raises(SafetyGateError):
        api.set_expiration("rp-9", 48)


class CrtV4(FakeV4):
    """FakeV4 + CRT: per-disk changed regions between two recovery points."""

    def __init__(self, changes=None, fail=None, **kw):
        super().__init__(**kw)
        self.rp_seq, self.changes, self.fail, self.expired = 0, changes or {}, fail, []

    def create_recovery_point(self, vm, name, app_consistent):
        self.rp_seq += 1
        self.log.append(f"rp:{name}")
        return f"rp-{self.rp_seq}"

    def set_expiration(self, rp, hours):
        self.expired.append((rp, hours))

    def delete_recovery_point(self, rp):
        self.log.append(f"delete-rp:{rp}")

    def changed_regions(self, cur, ref, size):
        if self.fail:
            raise ProviderError(self.fail)
        self.log.append(f"crt:{ref['recovery_point']}->{cur['recovery_point']}:{cur['disk_recovery_point']}")
        return self.changes.get(cur["disk_recovery_point"], [])


def _snap(src, run):
    src.ctx.run_id = run
    return src.create_snapshot(src.find_vm("RIS-Sainath"), True)


def test_crt_checkpoints_retire_and_release(tmp_path):
    fv4 = CrtV4(changes={"drp-03": [(4096, 8192, "REGULAR"), (0, 4096, "ZEROED"), (SIZE - 10, 100, "REGULAR")]})
    ctx, src, tgt, e = make(tmp_path, fv4, crt_base_keep_hours=72)
    s1 = _snap(src, "run1")
    cp1 = src.checkpoint(s1)
    assert cp1.id == "rp-1" and cp1.data["vm_recovery_point"] == "vmrp-rp-1" and cp1.data["crt"]
    d3 = next(d for d in src.inspect_vm(src.find_vm("RIS-Sainath")).disks if d.order == 3)
    assert cp1.data["disks"][d3.id] == "drp-03"
    fv4.log.clear()
    base = src.retire_snapshot(s1, None)
    assert base.id == "rp-1" and fv4.expired == [("rp-1", 72.0)]
    assert "delete-vg" in fv4.log and not any(x.startswith("delete-rp") for x in fv4.log)   # RP kept as base
    s2 = _snap(src, "run2")
    cur = src.checkpoint(s2)
    got = src.changed_regions(d3, base, cur)
    assert [(r.offset, r.length) for r in got] == [(0, 12288), (SIZE - 10, 10)]    # merged, clipped to disk
    assert "crt:rp-1->rp-2:drp-03" in fv4.log
    nxt = src.retire_snapshot(s2, base)
    assert nxt.id == "rp-2" and "delete-rp:rp-1" in fv4.log                         # old base released
    src.release_checkpoint(nxt)
    assert "delete-rp:rp-2" in fv4.log
    src.close()


def test_crt_falls_back_cleanly(tmp_path):
    from vmigrate.core.errors import ChangeTrackingUnavailable
    from vmigrate.core.interfaces import CheckpointRef
    fv4 = CrtV4(fail="HTTP 404 recovery point not found")
    ctx, src, tgt, e = make(tmp_path, fv4)
    s1 = _snap(src, "run1")
    base = src.retire_snapshot(s1, None)
    s2 = _snap(src, "run2")
    d0 = src.inspect_vm(src.find_vm("RIS-Sainath")).disks[0]
    with pytest.raises(ChangeTrackingUnavailable, match="CRT failed"):          # expired base -> compare mode
        src.changed_regions(d0, base, src.checkpoint(s2))
    with pytest.raises(ChangeTrackingUnavailable, match="no Nutanix recovery point"):
        src.changed_regions(d0, CheckpointRef(id="x", data={}), src.checkpoint(s2))
    src.cleanup(s2)
    _, nocrt, _, _ = make(tmp_path / "b", CrtV4(), crt=False)
    s = _snap(nocrt, "run3")
    assert not nocrt.supports_cbt() and nocrt.retire_snapshot(s, None).data == {}   # crt off: RP deleted as before
