"""Purpose: KVM / libvirt source tests without a KVM host: domain XML -> IR mapping and refusals,
the NBD block-status client against a socket server speaking structured replies, and engine runs
through LibvirtSource with a fake host shell (virsh) for both read paths: a shut-off domain read with
qemu-nbd, and a running domain read through a pull-mode backup with checkpoint dirty bitmaps.

Usage: pytest tests/test_libvirt.py
"""

from __future__ import annotations

import base64
import logging
import os
import re
import socket
import struct
import tempfile
import threading
import xml.etree.ElementTree as ET

import pytest

from vmigrate.config.loader import EngineConfig, MigrationConfig
from vmigrate.core.context import RunContext
from vmigrate.core.interfaces import DiskReader
from vmigrate.core.errors import DiskCountMismatch, ProviderError, SafetyGateError
from vmigrate.core.nbd import STATE_DIRTY, NbdStatus
from vmigrate.core.ranges import ByteRange, normalize
from vmigrate.engine import MigrationRunner, StateStore
from vmigrate.engine.dag import DagRunner, MemoryTaskStore
from vmigrate.engine.replica import MemoryReplicaStore, ReplicaRunner, sync_dag
from vmigrate.engine.safety import check_disk_counts
from vmigrate.ir import DiskBus, Firmware, OsFamily
from vmigrate.providers.fake.provider import FakeTarget, PatternDisk
from vmigrate.providers.libvirt import mapping
from vmigrate.providers.libvirt.source import LibvirtSource

UNIT = 6 << 20
GRAIN = 64 << 10
TARGETS = ["vda", "sdb", "vdc"]

XML = """<domain type='kvm'>
  <name>web1</name><uuid>6f1b0c2e-1111-2222-3333-444455556666</uuid>
  <metadata><libosinfo:libosinfo xmlns:libosinfo="http://libosinfo.org/xmlns/libvirt/domain/1.0">
    <libosinfo:os id="http://ubuntu.com/ubuntu/22.04"/></libosinfo:libosinfo></metadata>
  <memory unit='KiB'>8388608</memory><vcpu placement='static'>4</vcpu>
  <os firmware='efi'><type arch='x86_64' machine='pc-q35-8.2'>hvm</type>
    <firmware><feature enabled='yes' name='secure-boot'/></firmware></os>
  <cpu mode='host-passthrough'><topology sockets='2' cores='2' threads='1'/></cpu>
  <devices>
    <disk type='file' device='disk'><driver name='qemu' type='qcow2'/>
      <source file='/var/lib/libvirt/images/web1.qcow2'/><target dev='vda' bus='virtio'/><boot order='1'/></disk>
    <disk type='block' device='disk'><driver name='qemu' type='raw'/>
      <source dev='/dev/vg0/web1-data'/><target dev='sdb' bus='scsi'/>
      <address type='drive' controller='0' bus='0' target='0' unit='1'/></disk>
    <disk type='file' device='cdrom'><source file='/iso/ubuntu.iso'/><target dev='sda' bus='sata'/><readonly/></disk>
    <disk type='file' device='disk'><driver name='qemu' type='raw'/>
      <source file='/var/lib/libvirt/images/web1-logs.img'/><target dev='vdc' bus='virtio'/></disk>
    <controller type='scsi' index='0' model='virtio-scsi'/>
    <interface type='network'><mac address='52:54:00:aa:bb:cc'/><source network='default'/><model type='virtio'/></interface>
    <interface type='bridge'><mac address='52:54:00:aa:bb:cd'/><source bridge='br-storage'/><model type='e1000'/></interface>
  </devices></domain>"""

BLKLIST = """ file   disk    vda   /var/lib/libvirt/images/web1.qcow2
 block  disk    sdb   /dev/vg0/web1-data
 file   cdrom   sda   /iso/ubuntu.iso
 file   disk    vdc   /var/lib/libvirt/images/web1-logs.img"""


def content(t: str, gen: int) -> PatternDisk:
    return PatternDisk(f"web1:{t}", UNIT, generation=gen)


# ---------------------------------------------------------------------- mapping

def test_mapping_disks_firmware_nics():
    caps = {t: UNIT for t in TARGETS}
    spec = mapping.to_vmspec(XML, caps, state="running")
    assert [d.label for d in spec.ordered_disks()] == TARGETS and spec.boot_disk().label == "vda"
    assert spec.disks[0].controller == "virtio-blk" and spec.disks[0].bus == DiskBus.VIRTIO
    assert spec.disks[1].controller == "virtio-scsi" and spec.disks[1].bus == DiskBus.SCSI
    assert spec.firmware == Firmware.UEFI and spec.secure_boot and spec.cpu.sockets == 2 and spec.cpu.total == 4
    assert spec.memory_bytes == 8 << 30 and spec.machine_type == "pc-q35-8.2"
    assert spec.guest_os.family == OsFamily.LINUX and spec.guest_os.distro == "ubuntu" and spec.guest_os.version == "22.04"
    assert [(n.network, n.model) for n in spec.nics] == [("default", "virtio"), ("br-storage", "e1000")]
    assert check_disk_counts(spec, mapping.crosscheck(XML, BLKLIST))
    with pytest.raises(DiskCountMismatch):
        check_disk_counts(spec, mapping.crosscheck(XML, BLKLIST.rsplit("\n", 1)[0]))


def test_mapping_refusals_and_xml_documents():
    lun = XML.replace("<disk type='block' device='disk'>", "<disk type='block' device='lun'>")
    with pytest.raises(mapping.LibvirtMappingError, match="pass-through"):
        mapping.to_vmspec(lun, {t: UNIT for t in TARGETS})
    with pytest.raises(mapping.LibvirtMappingError, match="capacity"):
        mapping.to_vmspec(XML, {"vda": UNIT})
    with pytest.raises(mapping.LibvirtMappingError, match="unsafe"):
        mapping.offline_plan({"target": "vda", "type": "file", "format": "raw", "source": "/x; rm -rf /"})
    with pytest.raises(mapping.LibvirtMappingError, match="shut-off"):
        mapping.offline_plan({"target": "vda", "type": "network", "protocol": "rbd", "format": "raw", "source": "p/i"})
    disks = [{"target": "vda"}, {"target": "sdb", "readonly": True}]
    b = ET.fromstring(mapping.backup_xml(disks, 10950, "/var/lib/libvirt/images", "vmigrate-r1", "vmigrate-r0"))
    assert b.get("mode") == "pull" and b.findtext("incremental") == "vmigrate-r0"
    assert b.find("server").attrib == {"transport": "tcp", "name": "127.0.0.1", "port": "10950"}
    assert b.find("./disks/disk[@name='vda']/scratch").get("file") == "/var/lib/libvirt/images/vmigrate-r1-vda.scratch"
    assert b.find("./disks/disk[@name='sdb']").get("backup") == "no"
    c = ET.fromstring(mapping.checkpoint_xml("vmigrate-r1", disks))
    assert c.findtext("name") == "vmigrate-r1" and c.find("./disks/disk[@name='vda']").get("checkpoint") == "bitmap"


# ---------------------------------------------------------------------- NBD block status (real protocol)

def _rx(c, n):
    b = b""
    while len(b) < n:
        x = c.recv(n - len(b))
        if not x:
            raise EOFError
        b += x
    return b


def _status_server(path, size, extents_by_ctx):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def reply(c, opt, rtype, payload=b""):
        c.sendall(struct.pack(">QIII", 0x3E889045565A9, opt, rtype, len(payload)) + payload)

    def run():
        c, _ = srv.accept()
        ctx_name = None
        try:
            c.sendall(b"NBDMAGIC" + struct.pack(">QH", 0x49484156454F5054, 3))
            _rx(c, 4)
            while True:
                _, opt, ln = struct.unpack(">QII", _rx(c, 16))
                data = _rx(c, ln) if ln else b""
                if opt == 8:
                    reply(c, 8, 1)
                elif opt == 10:
                    nl = struct.unpack(">I", data[:4])[0]
                    q = data[4 + nl + 8:].decode()
                    if q in extents_by_ctx:
                        ctx_name = q
                        reply(c, 10, 4, struct.pack(">I", 7) + q.encode())
                    reply(c, 10, 1)
                elif opt == 7:
                    reply(c, 7, 3, struct.pack(">HQH", 0, size, 1))
                    reply(c, 7, 1)
                    break
            while True:
                _m, _f, typ, h, off, ln = struct.unpack(">IHHQQI", _rx(c, 28))
                if typ == 2:
                    return
                ext = [(o, n, f) for o, n, f in extents_by_ctx[ctx_name] if o >= off][:2]   # answer in parts
                body = struct.pack(">I", 7) + b"".join(struct.pack(">II", n, f) for _o, n, f in ext)
                c.sendall(struct.pack(">IHHQI", 0x668E33EF, 1, 5, h, len(body)) + body)
        except EOFError:
            pass
        finally:
            c.close()
            srv.close()

    threading.Thread(target=run, daemon=True).start()


def test_nbd_block_status_dirty_bitmap():
    ctx = "qemu:dirty-bitmap:backup-vda"
    ext = [(0, GRAIN, 0), (GRAIN, 3 * GRAIN, STATE_DIRTY), (4 * GRAIN, GRAIN, 0), (5 * GRAIN, GRAIN, STATE_DIRTY),
           (6 * GRAIN, 10 * GRAIN, 0)]
    path = os.path.join(tempfile.mkdtemp(), "s")
    _status_server(path, 16 * GRAIN, {ctx: ext})
    with NbdStatus.connect_unix(path, "vda", ctx) as s:
        assert s.size == 16 * GRAIN
        assert s.ranges(lambda f: f & STATE_DIRTY) == [ByteRange(GRAIN, 3 * GRAIN), ByteRange(5 * GRAIN, GRAIN)]
    path2 = os.path.join(tempfile.mkdtemp(), "s")
    _status_server(path2, GRAIN, {"base:allocation": [(0, GRAIN, 0)]})
    with pytest.raises(OSError, match="meta context"):
        NbdStatus.connect_unix(path2, "vda", ctx)


# ---------------------------------------------------------------------- fake KVM host

class Reader(DiskReader):
    def __init__(self, disk: PatternDisk, host=None, base_gen=None, target=None):
        self.disk, self.host, self.base_gen, self.target = disk, host, base_gen, target
        self.closed = False

    @property
    def size(self):
        return self.disk.size

    def read(self, off, n):
        return self.disk.read(off, n)

    def allocated_ranges(self):
        return None

    def close(self):
        self.closed = True
        if self.host is not None:
            self.host.open_readers -= 1

    def status(self, ctx):
        assert ctx == f"qemu:dirty-bitmap:backup-{self.target}" and self.base_gen is not None
        disk = self.disk

        class S:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                pass

            def ranges(s, want):
                old = content(self.target, self.base_gen).read(0, UNIT)
                new = disk.read(0, UNIT)
                return normalize(ByteRange(i, GRAIN) for i in range(0, UNIT, GRAIN)
                                 if want(STATE_DIRTY if old[i:i + GRAIN] != new[i:i + GRAIN] else 0))
        return S()


class FakeHost:
    def __init__(self, state="running"):
        self.state, self.gen = state, 0
        self.files: dict[str, str] = {}
        self.checkpoints: dict[str, int] = {}     # name -> generation when created (ordered)
        self.job: dict | None = None
        self.calls: list[list[str]] = []
        self.frozen = 0
        self.thawed = 0
        self.open_readers = 0
        self.backup_xmls: list[ET.Element] = []

    def run(self, argv, check=True, timeout=None):
        self.calls.append(argv)
        if argv[0] == "virsh":
            assert argv[1:4] == ["-q", "-c", "qemu:///system"]
            return self.virsh(argv[4], argv[5:])
        if argv[:2] == ["sh", "-c"]:
            m = re.match(r"umask 077; echo (\S+) \| base64 -d > (\S+)$", argv[2])
            if m:
                self.files[m.group(2)] = base64.b64decode(m.group(1)).decode()
                return ""
            return "ok\n"
        return ""

    def fail(self, msg):
        raise ProviderError(f"kvm1: `virsh` failed (1): {msg}")

    def virsh(self, cmd, a):
        if cmd == "version":
            return "Compiled against library: libvirt 9.0.0\n"
        if cmd == "list":
            return "web1\nold-db\n"
        if cmd == "domname":
            return "web1\n" if a[0] == "6f1b0c2e-1111-2222-3333-444455556666" else ""
        if cmd == "dumpxml":
            return XML
        if cmd == "domblkinfo":
            return f"Capacity:       {UNIT}\nAllocation:     4096\nPhysical:       {UNIT}\n"
        if cmd == "domblklist":
            return BLKLIST
        if cmd == "domstate":
            return self.state + "\n\n"
        if cmd == "checkpoint-list":
            return "".join(n + "\n" for n in self.checkpoints)
        if cmd == "checkpoint-delete":
            self.checkpoints.pop(a[1], None)
            return ""
        if cmd == "domfsfreeze":
            self.frozen += 1
            return ""
        if cmd == "domfsthaw":
            self.thawed += 1
            return ""
        if cmd == "backup-begin":
            if self.job:
                self.fail("Operation not valid: domain already has a backup job")
            b = ET.fromstring(self.files[a[1]])
            c = ET.fromstring(self.files[a[2]])
            inc = b.findtext("incremental")
            if inc and inc not in self.checkpoints:
                self.fail(f"checkpoint not found: no domain checkpoint with matching name '{inc}'")
            assert self.frozen == self.thawed + (1 if self.frozen > self.thawed else 0)
            self.backup_xmls.append(b)
            self.job = {"gen": self.gen, "inc": inc, "port": int(b.find("server").get("port"))}
            self.checkpoints[c.findtext("name")] = self.gen
            return ""
        if cmd == "domjobinfo":
            return "Job type: Unbounded\nOperation: Backup\n" if self.job else "Job type: None\n"
        if cmd == "domjobabort":
            self.job = None
            return ""
        if cmd in ("shutdown", "destroy"):
            self.state = "shut off"
            return ""
        raise AssertionError(f"unexpected virsh {cmd} {a}")

    # factories
    def offline(self, shell, plan, tag, disk):
        assert self.state == "shut off" and tag.startswith("vmigrate-") and "--force-share" not in plan["nbd_extra"]
        assert plan["nbd_format"] == {"vda": "qcow2"}.get(disk.label, "raw")
        self.open_readers += 1
        return Reader(content(disk.label, self.gen), self)

    def backup(self, shell, port, target, disk):
        assert self.job and port == self.job["port"]
        self.open_readers += 1
        base = self.checkpoints.get(self.job["inc"]) if self.job["inc"] else None
        return Reader(content(target, self.job["gen"]), self, base_gen=base, target=target)


def make(tmp_path, host, **cfg):
    ctx = RunContext(dry_run=False, logger=logging.getLogger("t"))
    src = LibvirtSource("kvm", {"type": "libvirt", "host": "kvm1", **cfg}, ctx, shell=host,
                        offline_factory=host.offline, backup_factory=host.backup)
    tgt = FakeTarget("tgt", {"staging_dir": str(tmp_path / "tgt"), "persist": True}, ctx)
    e = EngineConfig(state_dir=tmp_path / "s", report_dir=tmp_path / "r", chunk_size=1 << 20, checkpoint_every=2 << 20,
                     verify_samples=4, verify_sample_bytes=64 << 10)
    return ctx, src, tgt, e


def job(**kw):
    return MigrationConfig(name="m", source="kvm", target="tgt", vm=kw.pop("vm", "web1"),
                           app_consistent=kw.pop("app_consistent", False), osmorph={"allow_manual": True}, **kw)


def test_find_vm_by_name_or_uuid(tmp_path):
    _, src, _, _ = make(tmp_path, FakeHost())
    assert [v.name for v in src.list_vms()] == ["web1", "old-db"]
    assert src.find_vm("6f1b0c2e-1111-2222-3333-444455556666").id == "web1"
    with pytest.raises(ProviderError):
        src.find_vm("nope")
    assert src.check_connection() == []


def test_shut_off_domain_read_with_image_lock(tmp_path):
    host = FakeHost(state="shut off")
    ctx, src, tgt, e = make(tmp_path, host)
    st = MigrationRunner(job(), e, src, tgt, ctx, StateStore(e.state_dir)).run()
    assert st.phase.value == "done"
    for i, t in enumerate(TARGETS):
        assert tgt._final(tgt.disks[f"web1-disk{i:02d}"]).read_bytes() == content(t, 0).read(0, UNIT)
    assert not any(c[4] == "backup-begin" for c in host.calls if c[0] == "virsh") and host.open_readers == 0


def test_running_domain_pull_backup_app_consistent(tmp_path):
    host = FakeHost()
    ctx, src, tgt, e = make(tmp_path, host)
    st = MigrationRunner(job(app_consistent=True), e, src, tgt, ctx, StateStore(e.state_dir)).run()
    assert st.phase.value == "done" and host.frozen == host.thawed == 1
    assert host.job is None and not host.checkpoints        # backup ended, migration's checkpoint deleted
    assert host.backup_xmls[0].findtext("incremental") is None
    for i, t in enumerate(TARGETS):
        assert tgt._final(tgt.disks[f"web1-disk{i:02d}"]).read_bytes() == content(t, 0).read(0, UNIT)
    assert host.open_readers == 0


def test_started_during_offline_copy_is_refused(tmp_path):
    host = FakeHost(state="shut off")
    ctx, src, tgt, e = make(tmp_path, host)
    vm = src.find_vm("web1")
    spec = src.inspect_vm(vm)
    snap = src.create_snapshot(vm, False)
    host.state = "running"
    with pytest.raises(SafetyGateError, match="started"):
        src.open_disk(snap, spec.disks[0])


def test_checkpoint_dirty_bitmap_replica(tmp_path):
    host, store = FakeHost(), MemoryReplicaStore()

    def sync():
        ctx, src, tgt, e = make(tmp_path, host)
        rr = ReplicaRunner(MigrationConfig(name="rep", source="kvm", target="tgt", vm="web1", app_consistent=False),
                          e, src, tgt, ctx, store)
        ts = MemoryTaskStore(sync_dag(False))
        assert DagRunner(ts, rr.sync_handlers()).run().value == "completed", [t.error for t in ts.load_tasks() if t.error]
        return rr, tgt

    sync()
    assert host.job is None and len(host.checkpoints) == 1          # job ended, checkpoint kept as the base
    first = next(iter(host.checkpoints))
    host.gen = 1
    rr, tgt = sync()
    last = rr.state.syncs[-1]
    assert host.backup_xmls[-1].findtext("incremental") == first
    assert last["modes"] == ["cbt"] and last["changed_bytes"] < 3 * UNIT
    assert first not in host.checkpoints and len(host.checkpoints) == 1   # older checkpoint deleted
    for i, t in enumerate(TARGETS):
        assert tgt._final(tgt.disks[f"rep-r{i:02d}"]).read_bytes() == content(t, 1).read(0, UNIT)
    # base checkpoint lost (e.g. deleted by an admin): the backup falls back to full, the sync to compare
    host.checkpoints.clear()
    host.gen = 2
    rr, tgt = sync()
    assert rr.state.syncs[-1]["modes"] != ["cbt"] and host.backup_xmls[-1].findtext("incremental") is None
    for i, t in enumerate(TARGETS):
        assert tgt._final(tgt.disks[f"rep-r{i:02d}"]).read_bytes() == content(t, 2).read(0, UNIT)


def test_cutover_shutdown(tmp_path):
    host = FakeHost()
    _, src, _, _ = make(tmp_path, host)
    vm = src.find_vm("web1")
    assert src.shutdown_vm(vm) == "web1 shut down" and host.state == "shut off"
    assert src.shutdown_vm(vm) == "web1 already shut off"
