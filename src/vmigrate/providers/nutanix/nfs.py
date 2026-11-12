"""Purpose: the Nutanix source's automatic NFS access, so endpoints need NO mount configuration. The
worker mounts each storage container it needs, read-only, from the cluster it already talks to
(<prism_element host>:/<container>), reads the snapshot clone's vmdisk files, and unmounts what IT
mounted when the run ends.

Usage:
    m = NfsMounter("10.0.0.10")                    # host = Prism Element host unless nfs_host is set
    path = m.path_for("ctr-prod")                  # -> /run/margarita/ntnx/ctr-prod (mounted on demand)
    m.unmount_all()

Why NFS: Prism has no API that streams a vmdisk's bytes; the container's NFS export is the direct
read path (pitfall 3: plain large reads, never qemu-img on these files). The cluster side needs one
thing: this worker's IP in the cluster's / container's NFS whitelist. A refused mount produces an
error that names the exact IP to whitelist.

Mount options: ro (never writes to the source), NFSv3 (Nutanix's standard export), hard + long
timeo (a CVM failover pauses reads instead of returning short reads), nolock (read-only).
Requires root (the worker runs as root in deploy.sh installs).
"""

from __future__ import annotations

import os
import re
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...core import cancel
from ...core.errors import ProviderError

BASE = "/run/margarita/ntnx"
OPTIONS = "ro,vers=3,nolock,hard,timeo=600,retrans=3,rsize=1048576"
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def local_ip_towards(host: str) -> str:
    """The IP this worker uses to reach `host` (no packets sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 2049))
            return s.getsockname()[0]
    except OSError:
        return "<this worker's IP>"


def is_mounted(path: str, mounts_file: str = "/proc/mounts") -> bool:
    try:
        with open(mounts_file) as f:
            return any(line.split()[1] == path for line in f if len(line.split()) > 1)
    except OSError:
        return False


class NfsMounter:
    def __init__(self, host: str, *, base: str = BASE, options: str = OPTIONS,
                 run: Callable[..., Any] = cancel.run, mounted: Callable[[str], bool] = is_mounted):
        self.host, self.base, self.options = host, base, options
        self._run, self._mounted = run, mounted
        self.ours: list[str] = []

    def path_for(self, container: str) -> Path:
        if not _SAFE.match(container):
            raise ProviderError(f"refusing unexpected container name {container!r}")
        mp = os.path.join(self.base, container)
        if self._mounted(mp):
            return Path(mp)                    # already mounted (by us earlier, or by an admin)
        os.makedirs(mp, mode=0o700, exist_ok=True)
        r = self._run(["mount", "-t", "nfs", "-o", self.options, f"{self.host}:/{container}", mp],
                      capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()[-400:]
            hint = ""
            if re.search(r"access denied|permission denied|not permitted", err, re.I):
                hint = (f" The cluster refused this worker: add {local_ip_towards(self.host)} to the NFS whitelist of "
                        f"container {container!r} (Prism > Storage > container > Filesystem Whitelist) or the cluster.")
            elif "only root" in err or os.geteuid() != 0:
                hint = " Mounting needs root: run the worker as root (deploy.sh does), or pre-mount and set nfs_root."
            raise ProviderError(f"cannot mount {self.host}:/{container} read-only: {err or 'mount failed'}.{hint}")
        self.ours.append(mp)
        return Path(mp)

    def unmount_all(self) -> None:
        for mp in reversed(self.ours):
            self._run(["umount", mp], capture_output=True, text=True, timeout=120)
            try:
                os.rmdir(mp)
            except OSError:
                pass
        self.ours.clear()
