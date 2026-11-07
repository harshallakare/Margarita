"""Purpose: P2V Linux source — the remote disk reader (run locally against files), inventory ->
VmSpec, cold-mode checks, LVM-snapshot planning (composed disk images) and the snapshot/cleanup
command sequence against a scripted shell.

Usage: pytest tests/test_physical.py
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import sys

import pytest

from vmigrate.core.context import RunContext
from vmigrate.core.errors import DryRunViolation, SafetyGateError
from vmigrate.ir import DiskBus, Firmware
from vmigrate.providers.physical import inventory as inv_mod
from vmigrate.providers.physical.reader import PipeReader
from vmigrate.providers.physical.source import PhysicalSource

MiB, GiB = 1 << 20, 1 << 30


def local_spawn(path_map: dict[str, str] | None = None):
    """Run the reader with this Python, rewriting device paths to test files."""
    def spawn(argv):
        argv = list(argv)
        m = json.loads(base64.b64decode(argv[-1]))
        if path_map:
            m["base"] = path_map.get(m["base"], m["base"])
            m["overlays"] = [[a, n, path_map.get(p, p), s] for a, n, p, s in m.get("overlays", [])]
        argv[-1] = base64.b64encode(json.dumps(m).encode()).decode()
        return subprocess.Popen([sys.executable, *argv[1:]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
    return spawn


def test_reader_composes_overlays_and_encodes(tmp_path):
    base = tmp_path / "disk"
    base.write_bytes(b"B" * MiB + bytes(MiB) + bytes(range(256)) * 4096)        # raw | zeros | incompressible-ish
    snap = tmp_path / "snap"
    snap.write_bytes(b"S" * (2 * MiB))
    size = base.stat().st_size
    r = PipeReader(local_spawn(), {"base": str(base), "size": size,
                                   "overlays": [[512 * 1024, 4096, str(snap), MiB]]})
    try:
        assert r.size == size
        got = r.read(0, size)
        want = bytearray(base.read_bytes())
        want[512 * 1024:512 * 1024 + 4096] = b"S" * 4096
        assert got == bytes(want)
        assert r.read(MiB, MiB) == bytes(MiB)                                    # zero reply path
        assert r.read(size - 10, 100) == base.read_bytes()[-10:]                 # clipped at EOF
        assert r.read(size, 10) == b""
    finally:
        r.close()
    bad = PipeReader(local_spawn(), {"base": str(base), "size": size + 4096})    # claims more than the file has
    with pytest.raises(Exception, match="short read"):
        bad.read(size, 4096)
    bad.close()


INV = {
    "hostname": "db01.example", "efi": True, "cpus": 16, "mem_kb": 64 * 1024 * 1024,
    "os_release": 'ID="rhel"\nVERSION_ID="8.9"\nID_LIKE="fedora"\n', "redhat_release": "",
    "disks": [
        {"name": "sda", "dev": "8:0", "size": 100 * GiB, "removable": False, "model": "PERC H740", "serial": "S1",
         "wwid": "naa.600a", "transport": "scsi", "holders": [],
         "parts": [{"name": "sda1", "dev": "8:1", "start": MiB, "size": GiB, "holders": []},
                   {"name": "sda2", "dev": "8:2", "start": GiB + MiB, "size": 99 * GiB - MiB, "holders": ["dm-0", "dm-1"]}]},
        {"name": "sdb", "dev": "8:16", "size": 500 * GiB, "removable": False, "model": "PERC H740", "serial": "S2",
         "wwid": "", "transport": "scsi", "holders": ["dm-2"], "parts": []},
        {"name": "sdc", "dev": "8:32", "size": 32 * GiB, "removable": True, "model": "USB", "serial": "",
         "wwid": "", "transport": "usb", "holders": [], "parts": []},
    ],
    "dm": [{"name": "dm-0", "dev": "253:0", "dmname": "rhel-root", "uuid": "LVM-aaa", "slaves": ["sda2"]},
           {"name": "dm-1", "dev": "253:1", "dmname": "rhel-swap", "uuid": "LVM-bbb", "slaves": ["sda2"]},
           {"name": "dm-2", "dev": "253:2", "dmname": "data-pg", "uuid": "LVM-ccc", "slaves": ["sdb"]}],
    "mounts": [{"dev": "253:0", "mnt": "/", "opts": "rw,relatime", "fstype": "xfs", "source": "/dev/mapper/rhel-root"},
               {"dev": "8:1", "mnt": "/boot", "opts": "rw,relatime", "fstype": "xfs", "source": "/dev/sda1"},
               {"dev": "253:2", "mnt": "/var/lib/pgsql", "opts": "rw", "fstype": "xfs", "source": "/dev/mapper/data-pg"}],
    "nics": [{"name": "eno1", "mac": "AA:BB:CC:00:00:01", "driver": "tg3"}],
    "ipv4": [["eno1", "10.1.1.5/24"]],
    "lsblk_disks": ["sda", "sdb"],
}
EXT = 4 * MiB
PVSEGS = [
    {"pv_name": "/dev/sda2", "pe_start": MiB, "vg_extent_size": EXT, "pv_mda_count": 1, "pvseg_start": 0,
     "pvseg_size": 1000, "lv_name": "root", "vg_name": "rhel", "seg_start": 0, "vg_free": 10 * GiB},
    {"pv_name": "/dev/sda2", "pe_start": MiB, "vg_extent_size": EXT, "pv_mda_count": 1, "pvseg_start": 1000,
     "pvseg_size": 512, "lv_name": "swap", "vg_name": "rhel", "seg_start": 0, "vg_free": 10 * GiB},
    {"pv_name": "/dev/sda2", "pe_start": MiB, "vg_extent_size": EXT, "pv_mda_count": 1, "pvseg_start": 1512,
     "pvseg_size": 2560, "lv_name": "", "vg_name": "rhel", "seg_start": 0, "vg_free": 10 * GiB},
    {"pv_name": "/dev/sdb", "pe_start": MiB, "vg_extent_size": EXT, "pv_mda_count": 1, "pvseg_start": 0,
     "pvseg_size": 100000, "lv_name": "pg", "vg_name": "data", "seg_start": 0, "vg_free": 100 * GiB},
]
LVS = [{"lv_name": "root", "vg_name": "rhel", "lv_size": 1000 * EXT, "segtype": "linear"},
       {"lv_name": "swap", "vg_name": "rhel", "lv_size": 512 * EXT, "segtype": "linear"},
       {"lv_name": "pg", "vg_name": "data", "lv_size": 100000 * EXT, "segtype": "linear"}]


def test_inventory_to_vmspec_and_crosscheck():
    s = inv_mod.to_vmspec(INV)
    assert s.name == "db01.example" and s.cpu.total == 16 and s.memory_bytes == 64 * GiB and s.firmware == Firmware.UEFI
    assert [d.label for d in s.ordered_disks()] == ["/dev/sda", "/dev/sdb"]               # USB stick excluded
    assert s.disks[0].is_boot and s.disks[0].id == "naa.600a" and s.disks[1].id == "S2"
    assert s.disks[0].bus == DiskBus.SCSI and s.disks[0].controller is None              # -> virtio injection
    assert s.guest_os.distro == "rhel" and s.nics[0].mac == "aa:bb:cc:00:00:01" and s.nics[0].ipv4 == "10.1.1.5"
    assert inv_mod.crosscheck(INV) == {"sysfs": 2, "lsblk": 2}
    assert inv_mod.cold_problems(INV, ["sda"]) and "rescue" in inv_mod.cold_problems(INV, ["sda"])[0]
    cold = json.loads(json.dumps(INV))
    cold["mounts"] = []
    assert inv_mod.cold_problems(cold, ["sda", "sdb"]) == []


def test_inventory_script_runs():
    out = subprocess.run([sys.executable, "-c", inv_mod.INVENTORY], capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    assert {"hostname", "disks", "mounts", "nics"} <= set(data)


def test_lvm_plan_composes_images():
    p = inv_mod.lvm_plan(INV, ["sda", "sdb"], PVSEGS, LVS, run_tag="vmigrate-r1")
    assert {(x["vg"], x["lv"]) for x in p["snapshots"]} == {("rhel", "root"), ("data", "pg")}   # swap: not mounted
    assert p["remount"] == ["/boot"]
    root = next(x for x in p["snapshots"] if x["lv"] == "root")
    assert root["cow_bytes"] == GiB                                                  # 10% < 1 GiB minimum
    sda = p["disks"]["sda"]["overlays"]
    hdr = [o for o in sda if o[2].endswith(".hdr")][0]
    assert hdr == [GiB + MiB, MiB, "/run/vmigrate/vmigrate-r1-sda2.hdr", 0]          # PV metadata from before
    lv = [o for o in sda if o[2] == "/dev/rhel/vmigrate-r1-root"][0]
    assert lv == [GiB + MiB + MiB, 1000 * EXT, "/dev/rhel/vmigrate-r1-root", 0]
    assert not any("swap" in o[2] for o in sda)


def test_lvm_plan_refusals():
    bad = json.loads(json.dumps(INV))
    bad["mounts"][0] = {"dev": "8:2", "mnt": "/", "opts": "rw", "fstype": "xfs", "source": "/dev/sda2"}
    with pytest.raises(SafetyGateError, match="root filesystem is on a plain partition"):
        inv_mod.lvm_plan(bad, ["sda"], [], [], run_tag="vmigrate-x")
    thin = [dict(x, segtype="thin") if x["lv_name"] == "pg" else x for x in LVS]
    with pytest.raises(SafetyGateError, match="thin"):
        inv_mod.lvm_plan(INV, ["sda", "sdb"], PVSEGS, thin, run_tag="vmigrate-x")
    tight = [dict(x, vg_free=GiB // 2) if x["vg_name"] == "rhel" else x for x in PVSEGS]
    with pytest.raises(SafetyGateError, match="VG rhel: snapshots need"):
        inv_mod.lvm_plan(INV, ["sda", "sdb"], tight, LVS, run_tag="vmigrate-x")
    with pytest.raises(SafetyGateError, match="remount_ro"):
        inv_mod.lvm_plan(INV, ["sda", "sdb"], PVSEGS, LVS, run_tag="vmigrate-x", remount_ro=False)


class ScriptShell:
    def __init__(self, inv, *, fail_on=None, path_map=None):
        self.inv, self.calls, self.fail_on = inv, [], fail_on
        self.spawn = local_spawn(path_map)

    def run(self, argv, *, check=True, timeout=None):
        self.calls.append(argv)
        if self.fail_on and argv[0] == self.fail_on:
            raise RuntimeError(f"{argv[0]} failed")
        if argv[:2] == ["python3", "-c"]:
            return json.dumps(self.inv)
        if argv[0] == "pvs":
            return json.dumps({"report": [{"pv": PVSEGS}]})
        if argv[0] == "lvs":
            return json.dumps({"report": [{"lv": LVS}]})
        return "Linux\npy3\n"

    def close(self):
        pass


def _src(shell, mode="lvm-snapshot", dry=False):
    return PhysicalSource("p", {"type": "physical", "host": "db01", "mode": mode}, RunContext(
        dry_run=dry, run_id="run123456789abc", logger=logging.getLogger("t")), shell=shell)


def test_lvm_snapshot_sequence_read_and_cleanup(tmp_path):
    sda = tmp_path / "sda"
    with open(sda, "wb") as f:                     # sparse 100 GiB stand-in: only the start matters here
        f.truncate(100 * GiB)
    hdr = tmp_path / "hdr"
    hdr.write_bytes(b"H" * MiB)
    rootsnap = tmp_path / "root"
    rootsnap.write_bytes(b"R" * (2 * MiB))
    sh = ScriptShell(INV, path_map={"/dev/sda": str(sda), "/run/vmigrate/vmigrate-run123456789-sda2.hdr": str(hdr),
                                    "/dev/rhel/vmigrate-run123456789-root": str(rootsnap)})
    src = _src(sh)
    vm = src.find_vm("db01")
    assert src.disk_count_crosscheck(vm) == {"sysfs": 2, "lsblk": 2}
    snap = src.create_snapshot(vm, True)
    kinds = [c[0] for c in sh.calls]
    assert kinds.index("dd") < kinds.index("mount") < kinds.index("lvcreate")       # header copy BEFORE snapshots
    assert ["mount", "-o", "remount,ro", "/boot"] in sh.calls
    assert sum(1 for c in sh.calls if c[0] == "lvcreate") == 2
    spec = src.inspect_vm(vm)
    with src.open_disk(snap, spec.disks[0]) as r:
        assert r.read(GiB + MiB, 16) == b"H" * 16                                    # PV header from the copy
        assert r.read(GiB + 2 * MiB, 16) == b"R" * 16                                # LV data from the snapshot
        assert r.read(0, 16) == bytes(16)                                            # raw device elsewhere
    sh.calls.clear()
    src.cleanup(snap)
    assert [c[:2] for c in sh.calls] == [["lvremove", "-f"], ["lvremove", "-f"], ["mount", "-o"], ["rm", "-f"]]
    assert ["mount", "-o", "remount,rw", "/boot"] in sh.calls


def test_snapshot_failure_rolls_back_and_dry_run_refused():
    sh = ScriptShell(INV, fail_on="lvcreate")
    src = _src(sh)
    with pytest.raises(RuntimeError, match="lvcreate failed"):
        src.create_snapshot(src.find_vm("db01"), True)
    assert ["mount", "-o", "remount,rw", "/boot"] in sh.calls and any(c[0] == "rm" for c in sh.calls)
    with pytest.raises(DryRunViolation):
        _src(ScriptShell(INV), dry=True).create_snapshot(VmRefFor(), True)
    with pytest.raises(SafetyGateError, match="rescue"):
        _src(ScriptShell(INV), mode="cold").create_snapshot(VmRefFor(), False)


def VmRefFor():
    from vmigrate.core.interfaces import VmRef
    return VmRef("db01.example", "db01.example", "physical")
