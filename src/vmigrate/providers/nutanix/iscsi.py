"""Purpose: worker-side iSCSI for the Nutanix transport: find this host's initiator name, log in to a
temporary Volume Group's targets on the cluster's Data Services IP, map each block device to the
Volume Group disk it carries, and log out afterwards (only of targets we logged in to).

Usage:
    s = IscsiSession("10.0.0.50")                  # portal = cluster Data Services IP (port 3260)
    s.initiator_name()                             # from /etc/iscsi/initiatorname.iscsi
    devs = s.login("vmigrate-abc")                 # targets whose IQN contains the VG name
    by_index = map_devices(vg_disks, devs, size_of)  # {vg disk index: /dev/disk/by-path/...}
    s.logout()

Device mapping (pure, unit-tested): Nutanix exposes a Volume Group either as one target with
LUN n = disk index, or as "virtual targets" (...-tgt<n>, LUN 0) spreading disks across targets.
Both layouts are handled; every mapping is verified by size and any ambiguity fails loud.
Requires open-iscsi (iscsiadm) and root on the worker.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from typing import Any

from ...core import cancel
from ...core.errors import ProviderError, SafetyGateError

_TGT = re.compile(r"-tgt(\d+)$")


def parse_sendtargets(out: str, portal: str, vg_name: str) -> list[str]:
    """`iscsiadm -m discovery -t sendtargets` lines -> IQNs of OUR volume group on this portal."""
    host = portal.split(":")[0]
    found = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        addr, iqn = parts
        if addr.split(",")[0].split(":")[0] == host and vg_name.lower() in iqn.lower():
            found.append(iqn)
    return sorted(set(found))


def by_path_devices(listing: list[str], portal: str, targets: list[str]) -> list[tuple[str, int, str]]:
    """Entries of /dev/disk/by-path -> [(target, lun, path)] for our targets (whole disks only)."""
    host = portal.split(":")[0]
    out = []
    for name in listing:
        m = re.match(rf"^ip-{re.escape(host)}:\d+-iscsi-(.+)-lun-(\d+)$", name)
        if m and m.group(1) in targets:
            out.append((m.group(1), int(m.group(2)), f"/dev/disk/by-path/{name}"))
    return sorted(out)


def map_devices(vg_disks: list[dict[str, Any]], devices: list[tuple[str, int, str]],
                size_of: Callable[[str], int]) -> dict[int, str]:
    """{vg disk index: device path}. Layout A: one target, LUN = index. Layout B: virtual targets
    '-tgt<n>' each carrying one disk at LUN 0 (n = order of disks by index). Verified by size."""
    if len(devices) != len(vg_disks):
        raise SafetyGateError(f"iSCSI shows {len(devices)} LUNs but the volume group has {len(vg_disks)} disks")
    want = sorted(vg_disks, key=lambda d: d["index"])
    targets = sorted({t for t, _, _ in devices})
    out: dict[int, str] = {}
    if len(targets) == 1:
        luns = {lun: p for _, lun, p in devices}
        for d in want:
            if d["index"] not in luns:
                raise SafetyGateError(f"no LUN {d['index']} for volume group disk {d['index']}")
            out[d["index"]] = luns[d["index"]]
    else:
        per = {}
        for t, lun, p in devices:
            m = _TGT.search(t)
            if not m or lun != 0:
                raise SafetyGateError(f"unexpected iSCSI layout: target {t} LUN {lun}")
            per[int(m.group(1))] = p
        for n, d in enumerate(want):
            if n not in per:
                raise SafetyGateError(f"no virtual target -tgt{n} for volume group disk {d['index']}")
            out[d["index"]] = per[n]
    for d in want:
        actual = size_of(out[d["index"]])
        if d.get("size") and actual != d["size"]:
            raise SafetyGateError(f"volume group disk {d['index']}: device {out[d['index']]} is {actual} bytes, "
                                  f"expected {d['size']} (refusing an ambiguous mapping)")
    return out


def block_size_of(path: str) -> int:
    fd = os.open(path, os.O_RDONLY)
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)


class IscsiSession:
    def __init__(self, portal: str, *, run: Callable[..., Any] = cancel.run,
                 listdir: Callable[[str], list[str]] = os.listdir, settle_timeout: float = 60, sleep: Any = cancel.sleep):
        self.portal = portal if ":" in portal else f"{portal}:3260"
        self._run, self._listdir, self.settle_timeout, self.sleep = run, listdir, settle_timeout, sleep
        self.logged_in: list[str] = []

    def _iscsiadm(self, *args: str, check: bool = True) -> str:
        r = self._run(["iscsiadm", *args], capture_output=True, text=True, timeout=120)
        if check and r.returncode != 0:
            raise ProviderError(f"iscsiadm {' '.join(args)} failed ({r.returncode}): {(r.stderr or r.stdout).strip()[-400:]}")
        return r.stdout or ""

    def initiator_name(self, path: str = "/etc/iscsi/initiatorname.iscsi") -> str:
        try:
            with open(path) as f:
                for line in f:
                    if line.strip().startswith("InitiatorName="):
                        return line.split("=", 1)[1].strip()
        except OSError as e:
            raise ProviderError(f"open-iscsi is not set up on this worker ({path}: {e}); install open-iscsi "
                                "(apt: open-iscsi, dnf: iscsi-initiator-utils)") from e
        raise ProviderError(f"no InitiatorName in {path}")

    def login(self, vg_name: str, expected: int) -> list[tuple[str, int, str]]:
        out = self._iscsiadm("-m", "discovery", "-t", "sendtargets", "-p", self.portal)
        targets = parse_sendtargets(out, self.portal, vg_name)
        if not targets:
            raise ProviderError(f"no iSCSI targets for {vg_name} on {self.portal} (initiator not attached yet, or the "
                                "Data Services IP is unreachable from this worker on port 3260)")
        for t in targets:
            self._iscsiadm("-m", "node", "-T", t, "-p", self.portal, "--login")
            self.logged_in.append(t)
        deadline = time.monotonic() + self.settle_timeout
        while True:   # wait for udev to create the by-path links for every LUN
            try:
                devs = by_path_devices(self._listdir("/dev/disk/by-path"), self.portal, targets)
            except OSError:
                devs = []
            if len(devs) >= expected or time.monotonic() > deadline:
                return devs
            self.sleep(1)

    def logout_matching(self, vg_name: str) -> None:
        """Log out of any session for `vg_name` (e.g. left behind by a crashed worker process)."""
        out = self._iscsiadm("-m", "session", check=False)
        for line in out.splitlines():
            m = re.search(r"\s(\S+:\d+),\d+\s+(\S+)", line)
            if m and vg_name.lower() in m.group(2).lower():
                self._iscsiadm("-m", "node", "-T", m.group(2), "-p", m.group(1), "--logout", check=False)
                self._iscsiadm("-m", "node", "-T", m.group(2), "-p", m.group(1), "-o", "delete", check=False)

    def logout(self) -> None:
        for t in reversed(self.logged_in):
            self._iscsiadm("-m", "node", "-T", t, "-p", self.portal, "--logout", check=False)
            self._iscsiadm("-m", "node", "-T", t, "-p", self.portal, "-o", "delete", check=False)
        self.logged_in.clear()
