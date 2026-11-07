"""Purpose: Windows -> KVM OSMorphing through virt-v2v-in-place (tool calls faked).

Usage: pytest tests/test_osmorph_windows.py
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

from vmigrate.core.context import RunContext
from vmigrate.ir import DiskBus, NicSpec, OsFamily
from vmigrate.osmorph import MorphRequest, resolve
from vmigrate.osmorph.windows_kvm import Tools, WindowsKvmStrategy, domain_xml, mac_args

from .test_pure_logic import spec


def _req(opts=None, images=True):
    s = spec(n=2)
    s.guest_os = s.guest_os.__class__(family=OsFamily.WINDOWS)
    s.disks[1].bus = DiskBus.SATA
    s.nics = [NicSpec(id="n0", network="v", mac="00:50:56:AA:BB:CC", ipv4="10.0.0.9", model="vmxnet3")]
    imgs = [(Path("/vol/d0.qcow2"), "qcow2"), (Path("/vol/d 1.raw"), "raw")] if images else []
    return MorphRequest(s, "kvm", DiskBus.SCSI, imgs, opts or {})


def _tools(have=("virt-v2v-in-place", "virt-customize"), files=("/usr/share/virtio-win",), rc=0):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw["env"]))
        xml = argv[argv.index("libvirtxml") + 1] if "libvirtxml" in argv else None
        if xml:
            calls[-1] = (argv, kw["env"], Path(xml).read_text())
        return subprocess.CompletedProcess(argv, rc, stdout="Finishing off\n", stderr="boom")
    t = Tools(which=lambda b: b if b in have else None, exists=lambda p: p in files, env={}, run=run)
    return t, calls


def test_domain_xml_and_mac_args():
    x = ET.fromstring(domain_xml(_req()))
    disks = x.findall("devices/disk")
    assert [d.find("source").get("file") for d in disks] == ["/vol/d0.qcow2", "/vol/d 1.raw"]
    assert [(d.find("target").get("dev"), d.find("target").get("bus")) for d in disks] == [("sda", "scsi"), ("sdb", "sata")]
    assert [d.find("driver").get("type") for d in disks] == ["qcow2", "raw"]
    assert mac_args({"00:50:56:AA:BB:CC": {"ip": "10.0.0.9", "gateway": "10.0.0.1", "prefix": 24,
                                           "dns": ["8.8.8.8"]}}) == [
        "--mac", "00:50:56:aa:bb:cc:ip:10.0.0.9,10.0.0.1,24,8.8.8.8"]


def test_registered_and_manual_without_tools():
    assert isinstance(resolve(OsFamily.WINDOWS, "kvm"), WindowsKvmStrategy)
    t, _ = _tools(have=(), files=())
    res = WindowsKvmStrategy(t).run(_req(), RunContext(dry_run=False, logger=logging.getLogger("t")))
    assert res.status == "manual_required" and "virt-v2v-in-place, virtio-win drivers" in res.notes[0]


def test_convert_runs_v2v_with_drivers_static_ip_and_firstboot():
    t, calls = _tools()
    opts = {"windows_ip": {"00:50:56:aa:bb:cc": {"ip": "10.0.0.9", "gateway": "10.0.0.1", "prefix": 24}},
            "firstboot_script": "ipconfig /all\n"}
    res = WindowsKvmStrategy(t).run(_req(opts), RunContext(dry_run=False, logger=logging.getLogger("t")))
    assert res.status == "applied", res.notes
    argv, env, xml = calls[0]
    assert argv[:3] == ["virt-v2v-in-place", "-i", "libvirtxml"] and "--mac" in argv
    assert env["VIRTIO_WIN"] == "/usr/share/virtio-win" and "/vol/d 1.raw" in xml
    fb_argv = calls[1][0]
    assert fb_argv[0] == "virt-customize" and fb_argv.count("-a") == 2 and "--firstboot" in fb_argv
    assert not any("windows-static-ip" in s for s in res.steps)


def test_old_virt_v2v_and_failure_and_static_ip_note():
    t, calls = _tools(have=("virt-v2v",), rc=1)
    import pytest
    with pytest.raises(RuntimeError, match="virt-v2v failed"):
        WindowsKvmStrategy(t).run(_req(), RunContext(dry_run=False, logger=logging.getLogger("t")))
    assert calls[0][0][:2] == ["virt-v2v", "--in-place"]
    res = WindowsKvmStrategy(_tools()[0]).run(_req(), RunContext(dry_run=True))
    assert res.status == "planned" and any("windows-static-ip" in s for s in res.steps)
