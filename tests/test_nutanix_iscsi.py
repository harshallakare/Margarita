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

    def request(self, method, url, json=None, headers=None, timeout=None, auth=None):
        path = url.split("9440", 1)[1]
        self.calls.append((method, path, json, headers, auth))
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


def test_v4_created_id_when_task_lists_only_the_vm():
    # seen on a real Prism Central: CreateRecoveryPoint task succeeded, entitiesAffected = [the VM] only
    only_vm = {"data": {"status": "SUCCEEDED", "entitiesAffected": [
        {"extId": "fd3b2bf4", "rel": "vmm:ahv:config:vm", "name": "ovin-jump"}]}}
    flt = f"{v4.RECOVERY_POINTS}?$filter=name%20eq%20'vmigrate-r'"
    s = Session({("POST", v4.RECOVERY_POINTS): Resp(202, {"data": {"extId": "t1"}}),
                 ("GET", f"{v4.TASKS}/t1"): Resp(200, only_vm),
                 ("GET", flt): Resp(200, {"data": [{"extId": "rp-77", "name": "vmigrate-r"},
                                                   {"extId": "rp-x", "name": "vmigrate-r-other"}]})})
    api = v4.PrismV4(s, "https://pc:9440", sleep=lambda x: None)
    assert api.create_recovery_point("fd3b2bf4", "vmigrate-r", app_consistent=True) == "rp-77"
    # completionDetails carries the ID on some versions; rel spellings vary
    assert v4.PrismV4._affected({"completionDetails": [{"name": "recoveryPointExtId", "value": "rp-5"}]},
                                "recovery-point") == "rp-5"
    assert v4.PrismV4._affected({"entitiesAffected": [{"rel": "dataprotection:config:recoveryPoint",
                                                       "extId": "rp-6"}]}, "recovery-point") == "rp-6"
    # $filter unsupported -> first page scanned; nothing matching -> loud error with the task
    s2 = Session({("POST", v4.RECOVERY_POINTS): Resp(202, {"data": {"extId": "t1"}}),
                  ("GET", f"{v4.TASKS}/t1"): Resp(200, only_vm),
                  ("GET", f"{v4.RECOVERY_POINTS}?$limit=100"): Resp(200, {"data": []})})
    with pytest.raises(ProviderError, match="created but its ID could not be determined.*ovin-jump"):
        v4.PrismV4(s2, "https://pc:9440", sleep=lambda x: None).create_recovery_point("vm", "vmigrate-r",
                                                                                      app_consistent=True)
    with pytest.raises(SafetyGateError):
        api.find_by_name("recovery-point", "prod-backup")      # only ever looks up vmigrate- objects


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
    def __init__(self, n=13, dsip="10.0.0.50", reachable=True, drop_disk=False, rp_id_lost=False,
                 no_disk_ext=False, vg_size=None, cdrom=False):
        self.n, self.dsip, self.reachable, self.drop_disk = n, dsip, reachable, drop_disk
        self.no_disk_ext, self.vg_size = no_disk_ext, vg_size   # Prism omits diskExtId / VG reports this size
        self.cdrom = cdrom                     # the RP also holds an ISO-backed CD-ROM copy (seen live)
        self.sizes = {"drp-cd": 1 << 20}
        self.rp_id_lost = rp_id_lost          # Prism created the RP but the ID could not be determined
        self.log: list[str] = []
        self.vg_map: dict[int, str] = {}
        self.named: dict[tuple[str, str], str] = {}

    def size_of_drp(self, drp):
        return self.sizes.get(drp, self.vg_size or SIZE)

    def find_by_name(self, kind, name):
        return self.named.get((kind, name))

    def clusters(self):
        if not self.reachable:
            raise ProviderError("Prism v4 API not available (needs Prism Central >= 7.3)")
        return [{"extId": "cl"}]

    def data_services_ip(self, cluster):
        return self.dsip if cluster == "cl" else None

    def create_recovery_point(self, vm, name, app_consistent):
        self.log.append(f"rp:{name}:{'app' if app_consistent else 'crash'}")
        if self.rp_id_lost:
            self.named[("recovery-point", name)] = "rp-orphan"
            raise ProviderError("recovery-point was created but its ID could not be determined")
        self.named[("recovery-point", name)] = "rp-1"      # like real Prism: findable by its name
        return "rp-1"

    def recovery_point_disks(self, rp):
        n = self.n - 1 if self.drop_disk else self.n
        d = {f"dev-vd-{i:02d}": f"drp-{i:02d}" for i in range(n)}       # v4 diskExtId = device uuid
        if self.cdrom:
            d = {"dev-cdrom": "drp-cd", **d}            # listed FIRST, so order alone would pick it
        return d

    def recovery_point_layout(self, rp):
        d = self.recovery_point_disks(rp)
        if self.no_disk_ext:
            return {"vm_recovery_point": f"vmrp-{rp}", "disks": {}, "unmapped": list(d.values())}
        return {"vm_recovery_point": f"vmrp-{rp}", "disks": d, "unmapped": []}

    def create_volume_group(self, name, cluster):
        self.log.append(f"vg:{name}")
        return "vg-1"

    def add_disk_from_recovery_point(self, vg, rp, drp, index):
        self.vg_map[index] = drp

    def vg_disks(self, vg):
        return [{"ext": f"vgd-{i}", "index": i, "size": self.size_of_drp(d)} for i, d in sorted(self.vg_map.items())]

    def attach_iscsi_client(self, vg, iqn):
        self.log.append(f"attach:{iqn}")

    def detach_iscsi_client(self, vg, iqn):
        self.log.append("detach")

    def delete_volume_group(self, vg):
        self.log.append("delete-vg")

    def delete_recovery_point(self, rp):
        self.log.append(f"delete-rp:{rp}" if rp != "rp-1" else "delete-rp")


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
                sz = fv4.size_of_drp(drp) if drp == "drp-cd" else SIZE
                p.write_bytes(PatternDisk(drp.replace("drp-", "vd-"), sz).read(0, sz))
                out.append((f"iqn.x:{vg_name}", idx, str(p)))
            return out

        def logout(self):
            fv4.log.append("logout")

        def logout_matching(self, name):
            pass
    return FakeIscsi


def make(tmp_path, fv4, transport="auto", disks=13, **cfg):
    ctx = RunContext(dry_run=False, logger=logging.getLogger("t"))
    src = NutanixSource("ahv", {"prism_element": "https://pe:9440", "prism_central": "https://pc:9440",
                                "username": "admin", "transport": transport, **cfg}, ctx,
                        client=FakePrism.with_vm(tmp_path, disks=disks, size=SIZE), v4=fv4,
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
    with pytest.raises(SafetyGateError, match="recovery point covers 12 disks, the VM has 13"):
        MigrationRunner(MigrationConfig(name="m", source="ahv", target="tgt", vm="RIS-Sainath"), e, src, tgt, ctx,
                        StateStore(e.state_dir)).run()
    assert "delete-rp" in fv4.log and not tgt.disks             # partial objects removed; nothing created


def _run(src, tgt, ctx, e):
    return MigrationRunner(MigrationConfig(name="m", source="ahv", target="tgt", vm="RIS-Sainath"), e, src, tgt, ctx,
                           StateStore(e.state_dir)).run()


def test_rp_without_disk_ext_ids_single_disk_maps_directly(tmp_path):
    # seen on a real Prism Central: diskRecoveryPoints carry only diskRecoveryPointExtId
    fv4 = FakeV4(n=1, no_disk_ext=True)
    ctx, src, tgt, e = make(tmp_path, fv4, disks=1)
    st = _run(src, tgt, ctx, e)
    assert st.phase.value == "done" and fv4.vg_map == {0: "drp-00"}
    assert tgt._final(tgt.disks["RIS-Sainath-disk00"]).read_bytes() == PatternDisk("vd-00", SIZE).read(0, SIZE)


def test_rp_with_iso_cdrom_copy_and_no_disk_ext_ids_matches_by_size(tmp_path):
    # seen live: "recovery point covers 2 disks, the VM has 1" (the extra copy is an ISO CD-ROM)
    fv4 = FakeV4(n=1, no_disk_ext=True, cdrom=True)
    ctx, src, tgt, e = make(tmp_path, fv4, disks=1)
    st = _run(src, tgt, ctx, e)
    assert st.phase.value == "done"
    assert sorted(fv4.vg_map.values()) == ["drp-00", "drp-cd"]            # both put in the VG ...
    assert tgt._final(tgt.disks["RIS-Sainath-disk00"]).read_bytes() == PatternDisk("vd-00", SIZE).read(0, SIZE)
    # ... and the identified copies (diskExtId present) never pull in the CD-ROM copy
    fv4 = FakeV4(n=1, cdrom=True)
    ctx, src, tgt, e = make(tmp_path / "b", fv4, disks=1)
    assert _run(src, tgt, ctx, e).phase.value == "done" and fv4.vg_map == {0: "drp-00"}


def test_rp_without_disk_ext_ids_equal_sizes_needs_explicit_trust(tmp_path):
    fv4 = FakeV4(no_disk_ext=True)                       # 13 disks, all the same size in this fake
    ctx, src, tgt, e = make(tmp_path, fv4)
    with pytest.raises(SafetyGateError, match="cannot be told apart.*rp_disk_order_trust"):
        _run(src, tgt, ctx, e)
    assert "delete-vg" in fv4.log and "delete-rp" in fv4.log and not tgt.disks
    fv4 = FakeV4(no_disk_ext=True)
    ctx, src, tgt, e = make(tmp_path / "b", fv4, rp_disk_order_trust=True)
    assert _run(src, tgt, ctx, e).phase.value == "done" and fv4.vg_map == {i: f"drp-{i:02d}" for i in range(13)}


def test_rp_order_mapping_is_refused_when_sizes_disagree(tmp_path):
    fv4 = FakeV4(n=1, no_disk_ext=True, vg_size=SIZE * 2)
    ctx, src, tgt, e = make(tmp_path, fv4, disks=1)
    with pytest.raises(SafetyGateError, match="no unlabelled recovery-point copy has that size"):
        _run(src, tgt, ctx, e)
    assert "delete-vg" in fv4.log and "delete-rp" in fv4.log


def test_v4_layout_asks_vm_recovery_point_endpoint_when_disk_ext_missing():
    rp = {"data": {"vmRecoveryPoints": [{"extId": "vmrp", "diskRecoveryPoints": [{"diskRecoveryPointExtId": "drp-1"}]}]}}
    full = {"data": {"extId": "vmrp", "diskRecoveryPoints": [{"diskRecoveryPointExtId": "drp-1", "diskExtId": "disk-a"}]}}
    s = Session({("GET", f"{v4.RECOVERY_POINTS}/rp"): Resp(200, rp),
                 ("GET", f"{v4.RECOVERY_POINTS}/rp/vm-recovery-points/vmrp"): Resp(200, full)})
    lay = v4.PrismV4(s, "https://pc:9440").recovery_point_layout("rp")
    assert lay == {"vm_recovery_point": "vmrp", "disks": {"disk-a": "drp-1"}, "unmapped": []}
    s = Session({("GET", f"{v4.RECOVERY_POINTS}/rp"): Resp(200, rp)})          # nobody knows: left unmapped
    lay = v4.PrismV4(s, "https://pc:9440").recovery_point_layout("rp")
    assert lay["disks"] == {} and lay["unmapped"] == ["drp-1"]


def test_recovery_point_with_unknown_id_is_still_cleaned_up(tmp_path):
    fv4 = FakeV4(rp_id_lost=True)
    ctx, src, tgt, e = make(tmp_path, fv4)
    with pytest.raises(ProviderError, match="could not be determined"):
        MigrationRunner(MigrationConfig(name="m", source="ahv", target="tgt", vm="RIS-Sainath"), e, src, tgt, ctx,
                        StateStore(e.state_dir)).run()
    assert "delete-rp:rp-orphan" in fv4.log                    # found by its vmigrate- name and deleted


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


def test_endpoint_level_preflight_does_not_demand_nfs_when_v4_is_available(tmp_path):
    # The dry-run's first step checks endpoints without a VM: auto + PC v4 => iSCSI, no PD needed.
    _, src, _, _ = make(tmp_path, FakeV4())
    assert src.preflight(None) == []
    _, src, _, _ = make(tmp_path / "b", FakeV4(reachable=False))
    p = src.preflight(None)
    assert len(p) == 1 and "protection_domain is not set" in p[0] and "fell back to NFS" in p[0]
    _, src, _, _ = make(tmp_path / "c", FakeV4(reachable=False), transport="iscsi")
    assert src.preflight(None)[0].startswith("transport iscsi:")


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
    assert isinstance(c0[0][4], v4.JwtOnly) and c0[0][4].jwt == "jwt1"


def test_crt_call_sends_only_the_session_token():
    # seen live (AOS 7.3): Basic auth + token cookie -> 403 with an empty body
    import requests
    s = requests.Session()
    s.auth = ("admin", "pc-secret")
    s.cookies.set("NTNX_IGW_SESSION", "pc-cookie")
    req = s.prepare_request(requests.Request("POST", "https://10.0.0.5:9440/x", json={}, auth=v4.JwtOnly("tok"),
                                             headers={"Cookie": "NTNX_IGW_SESSION=tok"}))
    assert "Authorization" not in req.headers and req.headers["Cookie"] == "NTNX_IGW_SESSION=tok"


def test_crt_compute_falls_back_to_a_version_the_cluster_serves():
    # seen live: v4.0 compute-changed-regions -> bare web-server 404 "The requested URL was not found on
    # the server"; the SDK's v4.4 path is what the cluster serves
    v44 = COMPUTE.replace("/v4.0/", "/v4.4/")
    route_404 = Resp(404, {"message": "The requested URL was not found on the server. If you entered the URL "
                                      "manually please check your spelling and try again."})
    ok = Resp(200, {"data": [{"offset": 0, "length": 8192}], "metadata": {"extraInfo": [{"name": "nextOffset",
                                                                                        "value": "0"}]}})
    disc = Resp(200, {"data": {"clusterIp": {"ipv4": {"value": "10.0.0.5"}}, "jwtToken": "j"}})
    s = Session({("POST", DISCOVER): disc, ("POST", COMPUTE): route_404, ("POST", v44): ok})
    api = v4.PrismV4(s, "https://pc:9440", sleep=lambda x: None)
    assert api.changed_regions(CUR, REF, 4 << 20) == [(0, 8192, "REGULAR")]
    assert [c[1] for c in s.calls if "compute" in c[1]] == [COMPUTE, v44]
    s.calls.clear()
    api.changed_regions(CUR, REF, 4 << 20)                              # remembered: straight to v4.4
    assert [c[1] for c in s.calls if "compute" in c[1]] == [v44]
    # a real API 404 (JSON error about the object) is not retried under other versions
    gone = Resp(404, {"data": {"error": [{"code": "DP-10400", "message": "Recovery point does not exist"}]}})
    s2 = Session({("POST", DISCOVER): disc, ("POST", COMPUTE): gone})
    with pytest.raises(ProviderError, match="DP-10400"):
        v4.PrismV4(s2, "https://pc:9440", sleep=lambda x: None).changed_regions(CUR, REF, 4 << 20)
    assert len([c for c in s2.calls if "compute" in c[1]]) == 1


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
        self.named[("recovery-point", name)] = f"rp-{self.rp_seq}"   # like real Prism: findable by name
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


def test_v4_detach_iscsi_client_uses_the_client_uuid():
    # seen live: detach with {"iscsiInitiatorName"} -> HTTP 400 VOL-40101 "the iSCSI client UUID must be
    # specified", and the volume group could then not be deleted (client still attached)
    vg = f"{v4.VOLUME_GROUPS}/vg-1"
    sent = []

    def detach(body):
        sent.append(body)
        if "extId" not in body:
            return Resp(400, {"data": {"error": [{"code": "VOL-40101"}]}})
        return Resp(202, {"data": {"extId": "t-d"}})
    done = {"data": {"status": "SUCCEEDED"}}
    s = Session({("GET", vg): Resp(200, {"data": {"name": "vmigrate-r"}}),
                 ("GET", f"{vg}/iscsi-client-attachments"): Resp(200, {"data": [
                     {"extId": "client-9", "clusterReference": "cl"}]}),
                 ("POST", f"{vg}/$actions/detach-iscsi-client"): detach,
                 ("GET", f"{v4.TASKS}/t-d"): Resp(200, done)})
    api = v4.PrismV4(s, "https://pc:9440", sleep=lambda x: None)
    api.detach_iscsi_client("vg-1", "iqn.1994-05.com.redhat:worker1")
    assert sent == [{"extId": "client-9"}]
    # attachment listing unavailable -> client found by its IQN
    flt = f"{v4.ISCSI_CLIENTS}?$filter=iscsiInitiatorName%20eq%20'iqn.x%3Aw1'"
    s2 = Session({("GET", vg): Resp(200, {"data": {"name": "vmigrate-r"}}),
                  ("GET", flt): Resp(200, {"data": [{"extId": "client-3", "iscsiInitiatorName": "iqn.x:w1"}]}),
                  ("POST", f"{vg}/$actions/detach-iscsi-client"): detach,
                  ("GET", f"{v4.TASKS}/t-d"): Resp(200, done)})
    sent.clear()
    v4.PrismV4(s2, "https://pc:9440", sleep=lambda x: None).detach_iscsi_client("vg-1", "iqn.x:w1")
    assert sent == [{"extId": "client-3"}]
    # never touches someone else's volume group
    s3 = Session({("GET", vg): Resp(200, {"data": {"name": "prod-vg"}})})
    with pytest.raises(SafetyGateError):
        v4.PrismV4(s3, "https://pc:9440").detach_iscsi_client("vg-1", "iqn.x:w1")


def test_v4_cleanup_is_safe_to_repeat_when_objects_are_already_gone():
    # seen live: a retried finalize hit DP-10400 "Recovery point ... does not exist" (removed by the
    # previous attempt) and failed; already-gone objects must count as cleaned up
    gone = Resp(404, {"data": {"error": [{"code": "DP-10400", "message": "does not exist in IDF"}]}})
    s = Session({("GET", f"{v4.RECOVERY_POINTS}/rp-x"): gone, ("GET", f"{v4.VOLUME_GROUPS}/vg-x"): gone})
    api = v4.PrismV4(s, "https://pc:9440")
    api.delete_recovery_point("rp-x")
    api.delete_volume_group("vg-x")
    api.detach_iscsi_client("vg-x", "iqn.x:w1")
    assert not [c for c in s.calls if c[0] != "GET"]               # nothing else attempted
    # other errors still surface
    s2 = Session({("GET", f"{v4.RECOVERY_POINTS}/rp-x"): Resp(403, {"error": "forbidden"})})
    with pytest.raises(ProviderError, match="HTTP 403"):
        v4.PrismV4(s2, "https://pc:9440").delete_recovery_point("rp-x")


def test_full_copy_reads_only_allocated_regions(tmp_path):
    """CRT with no reference = the disk's written regions: a full copy skips everything else."""
    class AllocV4(CrtV4):
        def changed_regions(self, cur, ref, size):
            assert ref is None
            self.log.append(f"alloc:{cur['disk_recovery_point']}")
            if self.fail:
                raise ProviderError(self.fail)
            return [(1 << 20, 4096, "REGULAR"), (0, 8192, "ZEROED"), (2 << 20, 1 << 20, "REGULAR")]
    fv4 = AllocV4()
    ctx, src, tgt, e = make(tmp_path, fv4, disks=2)
    snap = _snap(src, "run1")
    d0 = src.inspect_vm(src.find_vm("RIS-Sainath")).disks[0]
    with src.open_disk(snap, d0) as r:
        got = r.allocated_ranges()
    assert [(x.offset, x.length) for x in got] == [(1 << 20, 4096), (2 << 20, 1 << 20)]   # ZEROED dropped
    fv4.fail = "HTTP 500"
    with src.open_disk(snap, d0) as r:
        assert r.allocated_ranges() is None                       # falls back to reading everything
    src.close()
    off = make(tmp_path / "off", AllocV4(), disks=1, crt_allocation=False)[1]
    s = _snap(off, "run2")
    with off.open_disk(s, off.inspect_vm(off.find_vm("RIS-Sainath")).disks[0]) as r:
        assert r.allocated_ranges() is None
    off.close()
