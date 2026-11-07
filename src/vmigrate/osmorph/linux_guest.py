"""Purpose: pure helpers for deeper Linux OSMorphing — OS detection, network-config MAC unpinning,
hypervisor-tool package swap plans, SELinux relabel and the first-boot script runner.

Usage:
    info = detect_os({"/etc/os-release": text})            # -> OsInfo(distro, version, family, ...)
    new, changed = unpin_ifcfg(text, mac_map={...}, rename={...})
    new, changed = unpin_netplan(text, mac_map={...})
    files = firstboot_files([("10-agent.sh", script)])    # path -> (content, mode)

Everything here works on text so it is testable without a guest image; linux_kvm.py applies the
results through guestfish. Design follows the general OSMorphing approach (detect the distro,
make the network config hardware-independent, swap hypervisor tools, relabel); no code is
taken from other projects.

Network policy:
  * mac_map (old MAC -> new MAC) known (e.g. the target keeps the source MACs, or the operator
    supplied them): rewrite MACs in place, interface names are preserved exactly.
  * otherwise remove MAC pinning (HWADDR/MACADDR, NetworkManager mac-address, netplan
    match.macaddress, udev 70-persistent-net.rules) so the config applies to the new virtio NIC.
    A netplan entry that relied on `set-name` gets `match: {driver: virtio_net}` when it is the
    only ethernet, which keeps the old interface name.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any

# ----------------------------------------------------------------------------- OS detection
_DEBIAN = {"debian", "ubuntu", "linuxmint", "pop", "raspbian", "kali"}
_RHEL = {"rhel", "centos", "fedora", "rocky", "almalinux", "ol", "amzn", "scientific", "cloudlinux", "eurolinux"}
_SUSE = {"suse", "sles", "opensuse", "opensuse-leap", "opensuse-tumbleweed", "sled"}


@dataclass
class OsInfo:
    distro: str = "unknown"
    version: str = ""
    family: str = "unknown"          # debian | rhel | suse | unknown
    pretty: str = ""
    ids: set[str] = field(default_factory=set)

    @property
    def pkg(self) -> str:
        return {"debian": "dpkg", "rhel": "rpm", "suse": "rpm"}.get(self.family, "unknown")

    def to_dict(self) -> dict[str, Any]:
        return {"distro": self.distro, "version": self.version, "family": self.family, "pretty": self.pretty}


def _kv(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def detect_os(files: dict[str, str]) -> OsInfo:
    """files: path -> content for whichever of /etc/os-release, /etc/redhat-release,
    /etc/SuSE-release, /etc/debian_version exist in the guest."""
    info = OsInfo()
    if "/etc/os-release" in files:
        kv = _kv(files["/etc/os-release"])
        info.distro = kv.get("ID", "unknown").lower()
        info.version = kv.get("VERSION_ID", "")
        info.pretty = kv.get("PRETTY_NAME", "")
        info.ids = {info.distro, *kv.get("ID_LIKE", "").lower().split()}
    elif "/etc/redhat-release" in files:          # RHEL/CentOS 6
        t = files["/etc/redhat-release"].strip()
        info.pretty = t
        info.distro = "centos" if "centos" in t.lower() else "rhel"
        m = re.search(r"release (\d+(?:\.\d+)?)", t)
        info.version = m.group(1) if m else ""
        info.ids = {info.distro}
    elif "/etc/SuSE-release" in files:
        t = files["/etc/SuSE-release"]
        info.distro, info.pretty = "sles", t.splitlines()[0] if t else "SUSE"
        kv = {k.strip(): v.strip() for k, _, v in (ln.partition("=") for ln in t.splitlines()) if v}
        info.version = kv.get("VERSION", "") + (f".{kv['PATCHLEVEL']}" if kv.get("PATCHLEVEL") else "")
        info.ids = {"sles", "suse"}
    elif "/etc/debian_version" in files:
        info.distro, info.version, info.ids = "debian", files["/etc/debian_version"].strip(), {"debian"}
    if info.ids & _DEBIAN:
        info.family = "debian"
    elif info.ids & _RHEL:
        info.family = "rhel"
    elif info.ids & _SUSE or any(i.startswith("opensuse") for i in info.ids):
        info.family = "suse"
    return info


# ----------------------------------------------------------------------------- network
_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def norm_mac(m: str) -> str:
    m = m.strip().strip('"').strip("'").lower().replace("-", ":")
    return m


def unpin_ifcfg(text: str, *, mac_map: dict[str, str] | None = None,
                rename: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """RHEL/SUSE ifcfg-* : rewrite or drop HWADDR/MACADDR; rename DEVICE/NAME."""
    mac_map = {norm_mac(k): norm_mac(v) for k, v in (mac_map or {}).items()}
    rename = rename or {}
    out, changes = [], []
    for raw in text.splitlines(keepends=True):
        line = raw.rstrip("\n")
        key, sep, val = line.partition("=")
        k = key.strip()
        if sep and k in ("HWADDR", "MACADDR"):
            old = norm_mac(val)
            if old in mac_map:
                out.append(f"{k}={mac_map[old]}\n")
                changes.append(f"{k} {old} -> {mac_map[old]}")
            else:
                out.append(f"# {line}  # removed by Margarita: MAC changes on the target\n")
                changes.append(f"{k} {old} unpinned")
            continue
        if sep and k in ("DEVICE", "NAME") and val.strip().strip('"') in rename:
            new = rename[val.strip().strip('"')]
            out.append(f"{k}={new}\n")
            changes.append(f"{k} {val.strip()} -> {new}")
            continue
        out.append(raw)
    return "".join(out), changes


def unpin_nmconnection(text: str, *, mac_map: dict[str, str] | None = None,
                       rename: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """NetworkManager keyfile: mac-address / cloned-mac-address / interface-name."""
    mac_map = {norm_mac(k): norm_mac(v) for k, v in (mac_map or {}).items()}
    rename = rename or {}
    out, changes = [], []
    for raw in text.splitlines(keepends=True):
        k, sep, val = raw.rstrip("\n").partition("=")
        key = k.strip()
        if sep and key == "mac-address":
            old = norm_mac(val)
            if old in mac_map:
                out.append(f"mac-address={mac_map[old].upper()}\n")
                changes.append(f"mac-address {old} -> {mac_map[old]}")
            else:
                changes.append(f"mac-address {old} unpinned")
            continue
        if sep and key == "interface-name" and val.strip() in rename:
            out.append(f"interface-name={rename[val.strip()]}\n")
            changes.append(f"interface-name {val.strip()} -> {rename[val.strip()]}")
            continue
        out.append(raw)
    return "".join(out), changes


def unpin_netplan(text: str, *, mac_map: dict[str, str] | None = None,
                  rename: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """netplan YAML: rewrite or drop match.macaddress; keep set-name working on virtio.
    Comments are not preserved when something changes (the original is kept as a backup)."""
    import yaml  # PyYAML is a core dependency (config loader)

    doc = yaml.safe_load(text) or {}
    eths = ((doc.get("network") or {}).get("ethernets")) or {}
    mac_map = {norm_mac(k): norm_mac(v) for k, v in (mac_map or {}).items()}
    rename = rename or {}
    changes: list[str] = []
    for name in list(eths):
        e = eths[name] or {}
        m = e.get("match") or {}
        if "macaddress" in m:
            old = norm_mac(str(m["macaddress"]))
            if old in mac_map:
                m["macaddress"] = mac_map[old]
                changes.append(f"{name}: macaddress {old} -> {mac_map[old]}")
            else:
                del m["macaddress"]
                changes.append(f"{name}: macaddress {old} unpinned")
                if not m and e.get("set-name") and len(eths) == 1:
                    m["driver"] = "virtio_net"
                    changes.append(f"{name}: match driver virtio_net (keeps set-name {e['set-name']})")
            if m:
                e["match"] = m
            else:
                e.pop("match", None)
                if e.get("set-name"):          # nothing to match on any more: use the name itself
                    newname = rename.get(e["set-name"], e.pop("set-name"))
                    eths[newname] = eths.pop(name)
                    changes.append(f"{name}: becomes interface {newname}")
                    continue
        if name in rename and "match" not in e:
            eths[rename[name]] = eths.pop(name)
            changes.append(f"{name} -> {rename[name]}")
    if not changes:
        return text, []
    return yaml.safe_dump(doc, default_flow_style=False, sort_keys=False), changes


def udev_net_rules_to_remove(paths: list[str]) -> list[str]:
    """Persistent-net udev rules bind names to the OLD MACs; they are regenerated on boot."""
    return [p for p in paths if re.search(r"/(70|75)-persistent-net\.rules$", p)]


# ----------------------------------------------------------------------------- packages
HYPERVISOR_TOOLS = {
    "vmware": ["open-vm-tools", "open-vm-tools-desktop", "vmware-tools"],
    "hyperv": ["hyperv-daemons", "hyperv-daemons-license", "hypervkvpd", "hypervvssd", "hypervfcopyd",
               "hyperv-tools", "linux-cloud-tools-common"],
    "azure": ["WALinuxAgent", "walinuxagent"],
}
HYPERVISOR_UNITS = ["vmtoolsd.service", "vgauthd.service", "vmware-tools.service", "hv-kvp-daemon.service",
                    "hv-vss-daemon.service", "hv-fcopy-daemon.service", "hypervkvpd.service", "hypervvssd.service",
                    "waagent.service", "walinuxagent.service"]


def query_cmd(info: OsInfo) -> str:
    return "dpkg-query -W -f=${Package}\\n" if info.family == "debian" else "rpm -qa --qf %{NAME}\\n"


def tools_to_remove(installed: list[str]) -> list[str]:
    have = {p.strip() for p in installed if p.strip()}
    return sorted({p for pkgs in HYPERVISOR_TOOLS.values() for p in pkgs if p in have})


def remove_cmd(info: OsInfo, pkgs: list[str]) -> str:
    q = " ".join(shlex.quote(p) for p in pkgs)
    return f"dpkg --purge {q}" if info.family == "debian" else f"rpm -e --nodeps {q}"


def guest_agent_script(info: OsInfo) -> str:
    """First-boot: install + enable qemu-guest-agent with the guest's own package manager."""
    return """#!/bin/sh
# Margarita: install the QEMU guest agent on the new KVM platform (needs package repositories).
set -e
if command -v apt-get >/dev/null 2>&1; then
  DEBIAN_FRONTEND=noninteractive apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q qemu-guest-agent
elif command -v dnf >/dev/null 2>&1; then dnf -y install qemu-guest-agent
elif command -v yum >/dev/null 2>&1; then yum -y install qemu-guest-agent
elif command -v zypper >/dev/null 2>&1; then zypper -n install qemu-guest-agent
else echo "no supported package manager"; exit 1
fi
systemctl enable --now qemu-guest-agent 2>/dev/null || systemctl start qemu-guest-agent || true
"""


# ----------------------------------------------------------------------------- selinux
def selinux_enabled(config_text: str | None) -> bool:
    if not config_text:
        return False
    return _kv(config_text).get("SELINUX", "disabled").lower() in ("enforcing", "permissive")


# ----------------------------------------------------------------------------- first boot
FIRSTBOOT_DIR = "/var/lib/vmigrate/firstboot.d"
FIRSTBOOT_RUNNER = "/usr/local/sbin/vmigrate-firstboot"
FIRSTBOOT_UNIT = "/etc/systemd/system/vmigrate-firstboot.service"
FIRSTBOOT_WANTS = "/etc/systemd/system/multi-user.target.wants/vmigrate-firstboot.service"

_RUNNER = f"""#!/bin/sh
# Margarita first-boot runner: runs each script in {FIRSTBOOT_DIR} once, in name order.
# Successful scripts move to done/; failed ones stay and are retried on the next boot.
# When nothing is left the service disables itself.
D={FIRSTBOOT_DIR}
LOG=/var/log/vmigrate-firstboot.log
mkdir -p "$D/done"
for s in $(ls "$D" 2>/dev/null | grep '\\.sh$' | sort); do
  echo "$(date -Is) running $s" >> "$LOG"
  if sh "$D/$s" >> "$LOG" 2>&1; then
    mv "$D/$s" "$D/done/$s"; echo "$(date -Is) $s ok" >> "$LOG"
  else
    echo "$(date -Is) $s FAILED (will retry next boot)" >> "$LOG"
  fi
done
if ! ls "$D" 2>/dev/null | grep -q '\\.sh$'; then
  systemctl disable vmigrate-firstboot.service >/dev/null 2>&1 || rm -f {FIRSTBOOT_WANTS}
fi
exit 0
"""

_UNIT = f"""[Unit]
Description=Margarita post-migration first-boot tasks
After=network-online.target
Wants=network-online.target
ConditionDirectoryNotEmpty={FIRSTBOOT_DIR}

[Service]
Type=oneshot
ExecStart={FIRSTBOOT_RUNNER}
TimeoutSec=0

[Install]
WantedBy=multi-user.target
"""

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def firstboot_files(scripts: list[tuple[str, str]]) -> dict[str, tuple[str, int]]:
    """path -> (content, mode) for the runner, the unit and each numbered script."""
    files: dict[str, tuple[str, int]] = {FIRSTBOOT_RUNNER: (_RUNNER, 0o755), FIRSTBOOT_UNIT: (_UNIT, 0o644)}
    for i, (name, body) in enumerate(scripts):
        base = name if name.endswith(".sh") else f"{name}.sh"
        if not _SAFE_NAME.match(base):
            raise ValueError(f"first-boot script name {name!r}: use letters, digits, '.', '_' or '-'")
        if not re.match(r"^\d\d-", base):
            base = f"{50 + i:02d}-{base}"
        files[f"{FIRSTBOOT_DIR}/{base}"] = (body if body.endswith("\n") else body + "\n", 0o755)
    return files


def user_scripts(opts: dict[str, Any]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """osmorph.scripts: [{name, when: offline|firstboot, script}] -> (offline, firstboot).
    Also accepts the wizard's shorthand osmorph.offline_script / osmorph.firstboot_script."""
    off: list[tuple[str, str]] = []
    fb: list[tuple[str, str]] = []
    for i, s in enumerate(opts.get("scripts") or []):
        body = str(s.get("script") or "")
        if not body.strip():
            continue
        name = str(s.get("name") or f"user-{i + 1}")
        when = s.get("when", "firstboot")
        if when not in ("offline", "firstboot"):
            raise ValueError(f"osmorph.scripts[{i}].when must be 'offline' or 'firstboot'")
        (off if when == "offline" else fb).append((name, body))
    if str(opts.get("offline_script") or "").strip():
        off.append(("user-offline", opts["offline_script"]))
    if str(opts.get("firstboot_script") or "").strip():
        fb.append(("user-firstboot", opts["firstboot_script"]))
    return off, fb


def is_mac(s: str) -> bool:
    return bool(_MAC.match(norm_mac(s)))
