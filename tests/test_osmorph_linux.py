"""Purpose: deeper Linux OSMorphing — OS detection, network MAC handling, tool swap, fstab UUIDs,
user offline / first-boot scripts and SELinux relabel, run against an in-memory guest filesystem.

Usage: pytest tests/test_osmorph_linux.py
"""

from __future__ import annotations

import logging

import pytest

from vmigrate.core.context import RunContext
from vmigrate.ir import DiskBus, NicSpec, OsFamily
from vmigrate.osmorph import MorphRequest
from vmigrate.osmorph import linux_guest as lg
from vmigrate.osmorph.linux_kvm import LinuxKvmStrategy

from .test_pure_logic import spec


class MemGuest:
    """Guestfish stand-in over a dict filesystem. `commands` records in-guest commands."""

    def __init__(self, files: dict[str, str], *, installed: list[str] | None = None,
                 uuids: dict[str, str] | None = None, fail_remove: bool = False):
        self.files = dict(files)
        self.modes: dict[str, int] = {}
        self.links: dict[str, str] = {}
        self.commands: list[str] = []
        self.scripts: list[str] = []
        self.installed = installed or []
        self.uuids = uuids or {}
        self.fail_remove = fail_remove

    # factory signature used by LinuxKvmStrategy
    def __call__(self, images, binary="guestfish"):
        return self

    def available(self) -> bool:
        return True

    def cat(self, p):
        return self.files[p]

    def upload_text(self, t, p):
        self.files[p] = t

    def write_file(self, p, t, mode=0o644):
        self.files[p], self.modes[p] = t, mode

    def exists(self, p):
        return p in self.files or p in self.links or self.is_dir(p)

    def is_dir(self, p):
        return any(k.startswith(p.rstrip("/") + "/") for k in list(self.files) + list(self.links))

    def ls(self, p):
        pre = p.rstrip("/") + "/"
        return sorted({k[len(pre):].split("/")[0] for k in list(self.files) + list(self.links) if k.startswith(pre)})

    def rm_f(self, p):
        self.files.pop(p, None)
        self.links.pop(p, None)

    def touch(self, p):
        self.files.setdefault(p, "")

    def ln_sf(self, target, link):
        self.links[link] = target

    def list_filesystems(self):
        return {d: "ext4" for d in self.uuids}

    def fs_uuids(self):
        return dict(self.uuids)

    def tune2fs_disable_periodic_fsck(self, devs):
        self.commands.append(f"tune2fs {devs}")

    def command(self, c, rw=True):
        self.commands.append(c)
        if c.startswith(("rpm -qa", "dpkg-query")):
            return "\n".join(self.installed)
        if c.startswith(("rpm -e", "dpkg --purge")) and self.fail_remove:
            raise RuntimeError("scriptlet failed")
        if c.startswith(("lsinitrd", "lsinitramfs")):
            return "virtio_scsi virtio_blk"
        return ""

    def run_script(self, body, timeout=3600):
        self.scripts.append(body)
        return "script ran"


RHEL = {
    "/etc/os-release": 'NAME="Rocky Linux"\nID="rocky"\nID_LIKE="rhel centos fedora"\nVERSION_ID="9.3"\n'
                       'PRETTY_NAME="Rocky Linux 9.3 (Blue Onyx)"\n',
    "/etc/sysconfig/network-scripts/ifcfg-ens192": "DEVICE=ens192\nBOOTPROTO=static\nIPADDR=10.0.0.5\n"
                                                    "HWADDR=00:50:56:AA:BB:CC\nONBOOT=yes\n",
    "/etc/sysconfig/network-scripts/ifcfg-lo": "DEVICE=lo\n",
    "/etc/NetworkManager/system-connections/Wired connection 1.nmconnection":
        "[connection]\nid=Wired connection 1\ninterface-name=ens192\n[ethernet]\nmac-address=00:50:56:AA:BB:CC\n",
    "/etc/udev/rules.d/70-persistent-net.rules": 'SUBSYSTEM=="net", ATTR{address}=="00:50:56:aa:bb:cc", NAME="eth0"\n',
    "/etc/udev/rules.d/99-custom.rules": "# keep\n",
    "/etc/fstab": "UUID=root / xfs defaults 0 1\n/dev/sdb1 /data ext4 defaults 0 2\n",
    "/etc/selinux/config": "SELINUX=enforcing\nSELINUXTYPE=targeted\n",
    "/usr/lib/systemd/systemd": "",
    "/etc/systemd/system/multi-user.target.wants/vmtoolsd.service": "",
    "/boot/vmlinuz-5.14.0": "",
}


def _req(opts=None, *, controller="virtio-scsi"):
    s = spec()
    s.guest_os = s.guest_os.__class__(family=OsFamily.LINUX)
    for d in s.disks:
        d.controller = controller
    s.nics = [NicSpec(id="n0", network="vlan10", mac="00:50:56:aa:bb:cc", model="vmxnet3")]
    return MorphRequest(s, "kvm", DiskBus.SCSI, [], opts or {})


def _run(g, opts=None, *, dry=False, controller="virtio-scsi"):
    return LinuxKvmStrategy(guestfish_factory=g).run(_req(opts, controller=controller),
                                                     RunContext(dry_run=dry, logger=logging.getLogger("t")))


def test_detect_os_variants():
    assert lg.detect_os({"/etc/os-release": RHEL["/etc/os-release"]}).to_dict() == {
        "distro": "rocky", "version": "9.3", "family": "rhel", "pretty": "Rocky Linux 9.3 (Blue Onyx)"}
    assert lg.detect_os({"/etc/os-release": "ID=ubuntu\nVERSION_ID=\"22.04\"\n"}).family == "debian"
    assert lg.detect_os({"/etc/os-release": "ID=\"opensuse-leap\"\nID_LIKE=\"suse opensuse\"\n"}).family == "suse"
    c6 = lg.detect_os({"/etc/redhat-release": "CentOS release 6.10 (Final)\n"})
    assert (c6.distro, c6.version, c6.family) == ("centos", "6.10", "rhel")
    assert lg.detect_os({"/etc/debian_version": "10.13\n"}).family == "debian"
    assert lg.detect_os({}).family == "unknown"


def test_network_text_rewrites():
    new, ch = lg.unpin_ifcfg(RHEL["/etc/sysconfig/network-scripts/ifcfg-ens192"])
    assert "\nHWADDR=" not in new and "# HWADDR=00:50:56:AA:BB:CC" in new and "IPADDR=10.0.0.5" in new
    new, ch = lg.unpin_ifcfg("HWADDR=00:50:56:AA:BB:CC\nDEVICE=ens192\n",
                             mac_map={"00:50:56:aa:bb:cc": "fa:16:3e:00:00:01"}, rename={"ens192": "eth0"})
    assert new == "HWADDR=fa:16:3e:00:00:01\nDEVICE=eth0\n" and len(ch) == 2
    nm, _ = lg.unpin_nmconnection("[ethernet]\nmac-address=00:50:56:AA:BB:CC\nmtu=1500\n")
    assert "mac-address" not in nm and "mtu=1500" in nm
    yml = ("network:\n  version: 2\n  ethernets:\n    eth0:\n      match:\n        macaddress: 00:50:56:aa:bb:cc\n"
           "      set-name: eth0\n      addresses: [10.0.0.5/24]\n")
    out, ch = lg.unpin_netplan(yml)
    import yaml
    e = yaml.safe_load(out)["network"]["ethernets"]["eth0"]
    assert e["match"] == {"driver": "virtio_net"} and e["set-name"] == "eth0" and e["addresses"] == ["10.0.0.5/24"]
    out, ch = lg.unpin_netplan(yml, mac_map={"00:50:56:AA:BB:CC": "fa:16:3e:00:00:02"})
    assert yaml.safe_load(out)["network"]["ethernets"]["eth0"]["match"]["macaddress"] == "fa:16:3e:00:00:02"
    assert lg.unpin_netplan("network:\n  ethernets:\n    ens3: {dhcp4: true}\n") == (
        "network:\n  ethernets:\n    ens3: {dhcp4: true}\n", [])
    assert lg.udev_net_rules_to_remove(["/etc/udev/rules.d/70-persistent-net.rules", "/x/99-a.rules"]) == [
        "/etc/udev/rules.d/70-persistent-net.rules"]


def test_user_scripts_and_firstboot_files():
    off, fb = lg.user_scripts({"scripts": [{"name": "fix", "when": "offline", "script": "echo hi"},
                                           {"name": "agent", "script": "echo boot"},
                                           {"name": "empty", "script": "  "}],
                               "firstboot_script": "echo wizard"})
    assert off == [("fix", "echo hi")] and [n for n, _ in fb] == ["agent", "user-firstboot"]
    files = lg.firstboot_files(fb)
    assert files[lg.FIRSTBOOT_UNIT][1] == 0o644 and files[lg.FIRSTBOOT_RUNNER][1] == 0o755
    assert f"{lg.FIRSTBOOT_DIR}/50-agent.sh" in files and f"{lg.FIRSTBOOT_DIR}/51-user-firstboot.sh" in files
    with pytest.raises(ValueError):
        lg.firstboot_files([("../evil", "x")])
    with pytest.raises(ValueError):
        lg.user_scripts({"scripts": [{"script": "x", "when": "sometimes"}]})


def test_full_linux_morph_on_rhel_guest():
    g = MemGuest(RHEL, installed=["bash", "open-vm-tools", "kernel"], uuids={"/dev/sda1": "u-root", "/dev/sdb1": "u-data"})
    res = _run(g, {"scripts": [{"name": "fix-repo", "when": "offline", "script": "sed -i s/a/b/ /etc/x"},
                               {"name": "notify", "script": "curl -s http://cmdb/done"}]})
    assert res.status == "applied", res.notes
    steps = "\n".join(res.steps)
    assert "Rocky Linux 9.3" in steps
    ifcfg = g.files["/etc/sysconfig/network-scripts/ifcfg-ens192"]
    assert "\nHWADDR=" not in ifcfg and "ifcfg-ens192.vmigrate-orig" in "".join(g.files)
    assert "ifcfg-lo.vmigrate-orig" not in "".join(g.files)
    assert "mac-address" not in g.files["/etc/NetworkManager/system-connections/Wired connection 1.nmconnection"]
    assert "/etc/udev/rules.d/70-persistent-net.rules" not in g.files and "/etc/udev/rules.d/99-custom.rules" in g.files
    assert "rpm -e --nodeps open-vm-tools" in g.commands
    assert g.files["/etc/fstab"].splitlines()[1].split() == ["UUID=u-data", "/data", "ext4", "defaults,nofail", "0", "0"]
    assert g.files["/etc/fstab.vmigrate-orig"] == RHEL["/etc/fstab"]
    assert g.scripts == ["sed -i s/a/b/ /etc/x"]
    fb = sorted(p for p in g.files if p.startswith(lg.FIRSTBOOT_DIR))
    assert fb == [f"{lg.FIRSTBOOT_DIR}/10-qemu-guest-agent.sh", f"{lg.FIRSTBOOT_DIR}/51-notify.sh"]
    assert g.links[lg.FIRSTBOOT_WANTS] == lg.FIRSTBOOT_UNIT
    assert "/.autorelabel" in g.files                                  # SELinux enforcing
    assert "selinux-relabel" in steps and "firstboot-scripts" in steps


def test_preserved_macs_keep_config_and_tool_removal_fallback():
    g = MemGuest(RHEL, installed=["open-vm-tools"], fail_remove=True)
    res = _run(g, {"preserve_mac": True, "install_guest_agent": False})
    assert res.status == "applied"
    assert "HWADDR=00:50:56:aa:bb:cc" in g.files["/etc/sysconfig/network-scripts/ifcfg-ens192"]   # same MAC kept
    assert "/etc/systemd/system/multi-user.target.wants/vmtoolsd.service" not in g.files       # disabled instead
    assert not any(p.startswith(lg.FIRSTBOOT_DIR) for p in g.files)                           # nothing to run


def test_debian_guest_without_selinux_or_systemd():
    files = {"/etc/os-release": "ID=ubuntu\nVERSION_ID=\"22.04\"\nPRETTY_NAME=\"Ubuntu 22.04\"\n",
             "/etc/fstab": "UUID=r / ext4 defaults 0 1\n", "/boot/vmlinuz-6.5": ""}
    g = MemGuest(files, installed=["hyperv-daemons"])
    res = _run(g, {"install_guest_agent": False})
    assert "dpkg --purge hyperv-daemons" in g.commands and "/.autorelabel" not in g.files
    assert res.status == "applied"
    # a requested first-boot script on a guest without systemd fails loudly
    with pytest.raises(RuntimeError, match="no systemd"):
        _run(MemGuest(files), {"firstboot_script": "echo x"})


def test_dry_run_plan_lists_user_scripts_and_offline_script_needs_tooling():
    res = _run(MemGuest(RHEL), {"offline_script": "echo x"}, dry=True)
    assert res.status == "planned"
    assert any("offline-script:user-offline" in s and "REQUIRED/auto" in s for s in res.steps)

    class NoTool(MemGuest):
        def available(self):
            return False
    res = _run(NoTool(RHEL), {"offline_script": "echo x"}, dry=True)
    assert res.status == "manual_required"                              # can't run the operator's script here


def test_guestfish_write_file_sends_c_style_octal_mode(monkeypatch):
    """Seen live: `chmod 0o755` (Python's #o format) failed in guestfish with "invalid integer
    parameter"; guestfish wants C-style octal."""
    from vmigrate.osmorph.guestfs import Guestfish, octal_mode
    assert octal_mode(0o755) == "0755" and octal_mode(0o644) == "0644" and octal_mode(0o100600) == "0600"
    scripts: list[str] = []
    gf = Guestfish([])
    monkeypatch.setattr(gf, "_run", lambda script, **kw: scripts.append(script) or "")
    gf.write_file("/etc/vmigrate/firstboot.sh", "#!/bin/sh\n", 0o755)
    assert 'chmod 0755 "/etc/vmigrate/firstboot.sh"' in scripts[0] and "0o" not in scripts[0]
