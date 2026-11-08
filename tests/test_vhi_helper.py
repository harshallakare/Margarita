"""Purpose: VHI `helper_vm` write mode (Coriolis-style): a temporary helper VM in VHI gets the new
volumes attached and the worker writes/reads/morphs them over SSH through the NBD agent.

Faked: OpenStack (FakeHelperOpenStack) and SSH (LocalTransport runs the REAL nbd_agent.py as a local
subprocess against per-volume files), so the NBD protocol, writer/reader and bridge are exercised for real.

Usage: pytest tests/test_vhi_helper.py
"""

from __future__ import annotations

import base64
import logging
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from vmigrate.config.loader import EngineConfig, MigrationConfig
from vmigrate.core.context import RunContext
from vmigrate.core.errors import ConfigError, DryRunViolation, ProviderError
from vmigrate.core.interfaces import DiskHints
from vmigrate.engine import MigrationRunner, Phase, StateStore
from vmigrate.ir import DiskBus
from vmigrate.osmorph.windows_kvm import domain_xml
from vmigrate.osmorph.base import MorphRequest
from vmigrate.providers.fake.provider import FakeSource, PatternDisk
from vmigrate.providers.vhi import helper as H
from vmigrate.providers.vhi.nbd import NbdClient, NbdDiskReader
from vmigrate.providers.vhi.target import VhiTarget

from .conftest import fake_vm
from .fakes_platform import FakeOpenStack, FakeQemu


class FakeHelperOpenStack(FakeOpenStack):
    """Adds the helper-VM surface; attached volumes are backed by files under devdir."""

    def __init__(self, vstorage: Path, devdir: Path):
        super().__init__(vstorage)
        self.devdir = devdir
        self.devdir.mkdir(parents=True, exist_ok=True)
        self.sgs: dict[str, dict] = {}
        self.project_sgs = {"default", "ssh-from-dc"}                   # pre-existing in the project
        self.fips: dict[str, dict] = {}
        self.log: list[str] = []

    def create_volume(self, name, size_gib, volume_type, metadata, availability_zone=None, source_volid=None):
        vid = self._id("vol")
        v = {"id": vid, "name": name, "size": size_gib, "status": "available", "metadata": dict(metadata),
             "attachments": [], "volume_type": volume_type or "__default__"}
        self.volumes[vid] = v
        with open(self.dev(vid), "wb") as f:
            f.truncate(size_gib << 30)                 # a fresh thin volume reads as zeros
        return dict(v)

    def dev(self, vid: str) -> Path:
        return self.devdir / vid

    def find_image(self, n): return {"id": "img-1", "name": n} if n == "ubuntu-22.04" else None
    def find_network(self, n):
        return {"id": f"net-{n}", "name": n} if n.startswith("uuid-") or n in ("helper-net", "public") else None
    def flavors(self): return super().flavors() + [{"id": "f-small", "vcpus": 2, "ram": 2048}]
    def find_flavor(self, n): return next((f for f in self.flavors() if f["id"] == n), None)

    def create_server(self, **kw):
        if kw["block_device_mapping"][0]["source_type"] == "image":       # the helper
            sid = self._id("helper")
            self.servers[sid] = {"id": sid, "name": kw["name"], "status": "ACTIVE", "kw": kw, "atts": [],
                                 "metadata": kw.get("metadata", {}),
                                 "addresses": {"helper-net": [{"addr": "10.9.0.5", "version": 4}]}}
            self.log.append("create-helper")
            return {"id": sid, "status": "BUILD"}
        for b in kw["block_device_mapping"]:
            assert self.volumes[b["uuid"]]["status"] == "available", "volume still attached to the helper"
        return super().create_server(**kw)

    def attach_volume(self, sid, vid):
        super().attach_volume(sid, vid)
        self.volumes[vid]["attachments"] = [{"server_id": sid}]
        self.log.append(f"attach:{vid}")

    def detach_volume(self, sid, vid):
        s = self.servers[sid]
        s["atts"] = [a for a in s["atts"] if a["volume_id"] != vid]
        self.volumes[vid]["status"] = "available"
        self.volumes[vid]["attachments"] = []
        self.log.append(f"detach:{vid}")

    def delete_server(self, sid):
        self.log.append(f"delete-server:{self.servers[sid]['name']}")
        super().delete_server(sid)

    def server_ports(self, sid): return [{"id": f"port-{sid}"}]
    def create_floating_ip(self, net, port):
        fid = self._id("fip"); self.fips[fid] = {"net": net, "port": port}
        return {"id": fid, "floating_ip_address": "203.0.113.7"}
    def delete_floating_ip(self, fid): self.fips.pop(fid, None)
    def create_ssh_security_group(self, name, cidr):
        gid = self._id("sg"); self.sgs[gid] = {"name": name, "cidr": cidr}
        return {"id": gid, "name": name}
    def find_security_group(self, name):
        if name in self.project_sgs:
            return {"id": f"sg-{name}", "name": name}
        return next(({"id": k} for k, v in self.sgs.items() if v["name"] == name), None)
    def security_groups(self): return [{"id": f"sg-{n}", "name": n} for n in sorted(self.project_sgs)]
    def servers_by_name(self, name): return [dict(s) for s in self.servers.values() if s["name"] == name]
    def delete_security_group(self, gid): self.sgs.pop(gid, None)


def make_transport(api: FakeHelperOpenStack, hosts: list):
    class LocalTransport:
        """Stands in for SshTransport: the agent runs locally against the volume's backing file."""

        def __init__(self, host, key, known, port=22):
            hosts.append(host)
            self.known = Path(known)

        def _attached(self):
            return [a["volume_id"] for s in api.servers.values() if s["name"].startswith("vmigrate-helper")
                    for a in s["atts"]]

        def run(self, cmd, timeout=120):
            if cmd.startswith("ls -1 /dev/disk/by-id/"):
                return "".join(f"virtio-{v[:20]}\nvirtio-{v[:20]}-part1\n" for v in self._attached())
            if cmd.startswith("command -v python3"):
                return "/usr/bin/python3\n"
            if cmd.startswith("test -b "):
                return "ok\n" if any(shlex.split(cmd)[2].endswith(v[:20]) for v in self._attached()) else ""
            return ""

        def popen(self, cmd):
            a = shlex.split(cmd)
            assert a[:4] == ["sudo", "-n", "python3", "-c"]
            vid = next(v for v in self._attached() if a[5].endswith(v[:20]))
            return subprocess.Popen([sys.executable, "-c", a[4], str(api.dev(vid)), a[6]],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    return LocalTransport


def fake_keygen(path: Path) -> str:
    path.write_text(f"PRIVATE KEY {path.name}\n")
    Path(f"{path}.pub").write_text(f"ssh-ed25519 AAAA{path.name} vmigrate-helper\n")
    return f"ssh-ed25519 AAAA{path.name} vmigrate-helper"


def _tgt(tmp_path, *, dry=False, api=None, run_id="run-abcdef123456", **cfg):
    ctx = RunContext(dry_run=dry, logger=logging.getLogger("t"), run_id=run_id)
    api = api or FakeHelperOpenStack(tmp_path / "vstorage-absent", tmp_path / "devs")
    conf = {"type": "vhi", "project_id": "proj-1", "vstorage_mount": str(tmp_path / "no-vstorage"),
            "staging_dir": str(tmp_path / "staging"), "poll_interval": 0, "op_timeout": 5, "helper_image": "ubuntu-22.04",
            "helper_network": "helper-net", "network_map": {"net0": "uuid-net0"}, **cfg}
    t = VhiTarget("vhi", conf, ctx, api=api, qemu=FakeQemu())
    hosts: list = []
    t.helper._tf = make_transport(api, hosts)
    t.helper._keygen = fake_keygen
    return t, api, hosts


def test_auto_picks_helper_when_vstorage_absent_and_vstorage_when_present(tmp_path):
    t, _, _ = _tgt(tmp_path)
    assert t.write_mode == "helper_vm"
    (tmp_path / "vs" / "vols/datastores/cinder").mkdir(parents=True)
    t2, _, _ = _tgt(tmp_path / "b", vstorage_mount=str(tmp_path / "vs"))
    assert t2.write_mode == "vstorage"
    t3, _, _ = _tgt(tmp_path / "c", vstorage_mount=str(tmp_path / "vs"), write_mode="helper_vm")
    assert t3.write_mode == "helper_vm"
    with pytest.raises(ConfigError, match="write_mode"):
        _tgt(tmp_path / "d", write_mode="nfs")


def test_preflight_and_connection_check_report_missing_helper_settings(tmp_path):
    t, _, _ = _tgt(tmp_path, helper_image="nope", helper_network="")
    probs = t.preflight(None)
    assert any("helper image 'nope' not found" in p for p in probs)
    assert any("set helper_network" in p for p in probs)
    assert not any("vStorage" in p for p in probs)            # no vStorage demand in helper mode
    ok, _, _ = _tgt(tmp_path / "b")
    assert ok.check_connection() == [] and ok.preflight(None) == []


def test_write_read_back_and_cleanup_through_helper(tmp_path):
    t, api, hosts = _tgt(tmp_path)
    ref = t.create_disk("vm-disk00", (1 << 30) - 5, DiskHints(bus=DiskBus.SCSI))
    pat = PatternDisk("seed", 4 << 20)
    with t.open_disk_writer(ref) as w:
        assert w.prezeroed and w.staged_path is None
        w.write(1 << 20, pat.read(0, 4 << 20))
    t.commit_disk(ref, None)
    with t.open_disk_reader(ref) as r:
        assert r.read(1 << 20, 4 << 20) == pat.read(0, 4 << 20) and r.read(0, 16) == bytes(16)
    assert api.dev(ref.id).read_bytes()[1 << 20:(1 << 20) + 64] == pat.read(0, 64)
    helper = next(s for s in api.servers.values() if s["name"] == "vmigrate-helper-run-abcdef12")
    ud = base64.b64decode(helper["kw"]["user_data"]).decode()
    assert "name: vmigrate" in ud and "ssh-ed25519 AAAAid_ed25519" in ud and "ed25519_private" in ud
    assert helper["kw"]["block_device_mapping"][0]["delete_on_termination"] is True
    known = (t.helper.dir / "known_hosts").read_text()
    assert known.startswith("vmigrate-helper ssh-ed25519 AAAAhost_ed25519")     # pinned host key
    assert hosts == ["10.9.0.5"] and not api.sgs                          # nothing created ...
    assert helper["kw"]["security_groups"] == [{"name": "default"}]          # ... the project's default group
    hdir = t.helper.dir
    t.close()
    assert not any(s["name"].startswith("vmigrate-helper") for s in api.servers.values())
    assert not api.sgs and not hdir.exists()                    # keys removed with the helper
    assert api.volumes[ref.id]["status"] == "available"


def test_incremental_writes_zeros_explicitly(tmp_path):
    t, api, _ = _tgt(tmp_path)
    ref = t.create_disk("d", 1 << 20, DiskHints(bus=DiskBus.SCSI))
    with t.open_disk_writer(ref) as w:
        w.write(0, b"\xff" * 8192)
    with t.open_disk_writer(ref, incremental=True) as w:
        assert not w.prezeroed
        w.write_zeroes(0, 4096)                      # a region that became zero on the source
    assert api.dev(ref.id).read_bytes()[:8192] == bytes(4096) + b"\xff" * 4096
    t.close()


def test_floating_ip_and_existing_security_group(tmp_path):
    t, api, hosts = _tgt(tmp_path, helper_floating_network="public", helper_security_groups=["ssh-from-dc"])
    ref = t.create_disk("d", 1 << 20, DiskHints(bus=DiskBus.SCSI))
    with t.open_disk_writer(ref) as w:
        w.write(0, b"x")
    assert hosts == ["203.0.113.7"] and len(api.fips) == 1 and not api.sgs
    helper = next(s for s in api.servers.values() if s["name"].startswith("vmigrate-helper"))
    assert helper["kw"]["security_groups"] == [{"name": "ssh-from-dc"}]
    t.close()
    assert not api.fips


def test_security_group_options(tmp_path):
    t, api, _ = _tgt(tmp_path, helper_security_groups="nope")
    assert any("security group 'nope' not found" in p for p in t.preflight(None))
    t, api, _ = _tgt(tmp_path / "b", helper_create_security_group=True, helper_ssh_cidr="10.65.49.92/32")
    ref = t.create_disk("d", 1 << 20, DiskHints(bus=DiskBus.SCSI))
    with t.open_disk_writer(ref) as w:
        w.write(0, b"x")
    assert [v["cidr"] for v in api.sgs.values()] == ["10.65.49.92/32"]         # only when asked for
    t.close()
    assert not api.sgs
    assert [g["name"] for g in t.list_security_groups()] == ["default", "ssh-from-dc"]   # default first


def test_cleanup_keeps_going_and_removes_duplicate_helpers(tmp_path):
    t, api, _ = _tgt(tmp_path)
    ref = t.create_disk("d", 1 << 20, DiskHints(bus=DiskBus.SCSI))
    with t.open_disk_writer(ref) as w:
        w.write(0, b"x")
    h = t.helper
    dup = dict(next(s for s in api.servers.values() if s["name"] == h.name), id="helper-dup")
    api.servers["helper-dup"] = dup                       # a retried create made a second helper

    def boom(sid, vid):
        raise ProviderError("Connection aborted")
    api.detach_volume = boom
    with pytest.raises(ProviderError, match="cleanup incomplete.*vmigrate-helper-run-abcdef12"):
        t.close()
    assert not api.servers_by_name(h.name)               # both helpers deleted despite the failure


def test_dry_run_never_creates_a_helper(tmp_path):
    t, api, _ = _tgt(tmp_path, dry=True)
    assert t.preflight(None) == []
    with pytest.raises(DryRunViolation):
        t.helper.ensure()
    assert not api.servers


def test_helper_refuses_unmanaged_volume_and_missing_python(tmp_path):
    t, api, _ = _tgt(tmp_path)
    ref = t.create_disk("d", 1 << 20, DiskHints(bus=DiskBus.SCSI))
    api.volumes[ref.id]["metadata"] = {}
    with pytest.raises(Exception, match="not vmigrate-managed"):
        t.open_disk_writer(ref)
    t.close()
    t2, api2, _ = _tgt(tmp_path / "b")
    orig = t2.helper._tf

    class NoPython(orig):
        def run(self, cmd, timeout=120):
            return "" if cmd.startswith("command -v python3") else super().run(cmd, timeout)
    t2.helper._tf = NoPython
    with pytest.raises(ProviderError, match="no python3"):
        t2.helper.ensure()


def test_morph_bridge_serves_the_volume_over_a_unix_socket(tmp_path):
    t, api, _ = _tgt(tmp_path)
    ref = t.create_disk("d", 1 << 20, DiskHints(bus=DiskBus.SCSI))
    with t.open_disk_writer(ref) as w:
        w.write(512, b"hello")
    uri, fmt = t.morph_image(ref)
    assert fmt == "raw" and uri.startswith("nbd://?socket=")
    import socket
    s = socket.socket(socket.AF_UNIX)
    s.connect(uri.split("socket=", 1)[1])
    f = s.makefile("rwb", buffering=0)
    with NbdDiskReader(NbdClient(f, f), 1 << 20) as r:     # what guestfish's qemu does
        assert r.read(512, 5) == b"hello"
    xml = domain_xml(MorphRequest(_spec(), "kvm", DiskBus.SCSI, [(uri, "raw")]))
    assert "protocol='nbd'" in xml and "transport='unix'" in xml
    t.close()


def _spec():
    from vmigrate.ir import CpuSpec, DiskSpec, VmSpec
    return VmSpec(name="w", source_id="1", cpu=CpuSpec(1, 1), memory_bytes=1 << 30,
                  disks=[DiskSpec(id="d0", bus=DiskBus.SCSI, size_bytes=1 << 20, order=0, is_boot=True)])


def test_full_migration_through_helper_vm(tmp_path):
    ctx = RunContext(dry_run=False, logger=logging.getLogger("helper-e2e"), run_id="run-abcdef123456")
    src = FakeSource("src", {"type": "fake", "vms": [fake_vm(disks=4, size="3MiB")]}, ctx)
    t, api, _ = _tgt(tmp_path)
    t.ctx = t.helper.ctx = ctx
    eng = EngineConfig(state_dir=tmp_path / "state", report_dir=tmp_path / "rep", chunk_size=1 << 20,
                       verify_samples=4, verify_sample_bytes=64 << 10, boot_timeout=1, max_parallel_disks=2,
                       verify_full=True)
    job = MigrationConfig(name="m", source="src", target="vhi", vm="RIS-Sainath", osmorph={"allow_manual": True},
                          target_options={"network_map": {}})
    st = MigrationRunner(job, eng, src, t, ctx, StateStore(eng.state_dir)).run()
    t.close()
    assert st.phase == Phase.DONE, st.errors
    assert all(d.verify["full"]["ok"] and d.verify["target_samples"]["ok"] for d in st.disks.values())
    vm = next(s for s in api.servers.values() if not s["name"].startswith("vmigrate-helper"))
    assert len(vm["kw"]["block_device_mapping"]) == 4
    # every volume was detached from the helper before the real VM was created, and the helper is gone
    assert api.log.count("create-helper") == 1                           # one helper despite parallel disks
    assert api.log.index("create-helper") < min(i for i, x in enumerate(api.log) if x.startswith("detach:"))
    assert any(x.startswith("delete-server:vmigrate-helper") for x in api.log)
    assert not list((tmp_path / "staging").glob("*.raw"))                 # nothing staged locally


def test_cloud_config_and_device_lookup():
    cc = H.cloud_config("ssh-ed25519 CLIENT x", "-----BEGIN-----\nabc\n-----END-----\n", "ssh-ed25519 HOST y")
    assert "    -----BEGIN-----\n    abc" in cc and "ssh_genkeytypes: []" in cc and "NOPASSWD" in cc
    vid = "0123456789abcdefghij-rest"
    assert H.find_device("virtio-0123456789abcdefghij\nvirtio-0123456789abcdefghij-part1\n", "", vid) \
        == "/dev/disk/by-id/virtio-0123456789abcdefghij"
    assert H.find_device("", "vdb 0123456789abcdefghij\n", vid) == "/dev/vdb"
    assert H.find_device("virtio-other\n", "", vid) is None


def test_replica_compare_sync_through_helper_is_byte_exact(tmp_path):
    # what a replica sync in compare mode does: read the replica back while writing only the changes
    from vmigrate.engine.copier import copy_disk
    t, api, _ = _tgt(tmp_path)
    size = 4 << 20
    ref = t.create_disk("r", size, DiskHints(bus=DiskBus.SCSI))
    gen0, gen1 = PatternDisk("s", size, generation=0), PatternDisk("s", size, generation=1)
    with t.open_disk_writer(ref) as w:                                   # initial full sync
        copy_disk(gen0, w, chunk_size=1 << 20)
    with t.open_disk_writer(ref, incremental=True) as w, t.open_disk_reader(ref) as cmp:
        stats = copy_disk(gen1, w, chunk_size=1 << 20, compare=cmp)
    assert api.dev(ref.id).read_bytes()[:size] == gen1.read(0, size)
    assert stats.bytes_unchanged + stats.bytes_written + stats.bytes_zero_skipped >= size - (1 << 20)
    t.close()


def test_helper_kept_between_runs_is_reused_with_its_volumes_attached(tmp_path):
    t, api, hosts = _tgt(tmp_path)
    ref = t.create_disk("d", 1 << 20, DiskHints(bus=DiskBus.SCSI))
    with t.open_disk_writer(ref) as w:
        w.write(0, b"a" * 4096)
    rec = t.keep_helper()
    t.close()                                                  # nothing is destroyed: it was handed over
    assert rec and list(rec["attached"]) == [ref.id] and rec["host"] == "10.9.0.5"
    assert api.volumes[ref.id]["status"] == "in-use" and Path(rec["dir"]).is_dir()
    t2, _, _ = _tgt(tmp_path, api=api, run_id="run-second00000")   # the next sync, same worker
    assert t2.adopt_helper(rec)
    with t2.open_disk_writer(ref, incremental=True) as w:
        w.write(4096, b"b" * 4096)
    assert api.log.count("create-helper") == 1 and api.log.count(f"attach:{ref.id}") == 1   # no boot, no attach
    assert api.dev(ref.id).read_bytes()[:8192] == b"a" * 4096 + b"b" * 4096
    rec2 = t2.keep_helper()
    t2.release_helper(rec2)                                     # e.g. before a deploy, or idle too long
    assert not any(s["name"].startswith("vmigrate-helper") for s in api.servers.values())
    assert api.volumes[ref.id]["status"] == "available" and not Path(rec["dir"]).exists()
    t2.release_helper(rec2)                                     # repeatable


def test_kept_helper_not_adopted_without_keys_or_when_gone(tmp_path):
    import shutil as _sh
    from vmigrate.core.errors import SafetyGateError
    t, api, _ = _tgt(tmp_path)
    ref = t.create_disk("d", 1 << 20, DiskHints(bus=DiskBus.SCSI))
    with t.open_disk_writer(ref) as w:
        w.write(0, b"x")
    rec = t.keep_helper()
    keys = Path(rec["dir"])
    _sh.copytree(keys, tmp_path / "keys-bak")
    _sh.rmtree(keys)                                            # another worker: keys not here
    t2, _, _ = _tgt(tmp_path, api=api, run_id="run-other000000")
    assert not t2.adopt_helper(rec)
    _sh.copytree(tmp_path / "keys-bak", keys)
    api.servers[rec["server_id"]]["status"] = "SHUTOFF"
    assert not _tgt(tmp_path, api=api, run_id="run-third000000")[0].adopt_helper(rec)
    api.servers[rec["server_id"]]["metadata"] = {}              # not ours: never deleted
    with pytest.raises(SafetyGateError, match="not a vmigrate helper"):
        t2.release_helper(rec)
