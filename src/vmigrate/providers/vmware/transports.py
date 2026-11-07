"""Purpose: the two ways the VMware source reads a disk AS OF a snapshot.

  vddk  (production) nbdkit's vddk plugin opens the snapshot's base disk through VMware's VDDK
        (transports nbdssl / nbd / hotadd) and exports it read-only on a private unix socket;
        core.nbd.NbdReader reads it. Same path virt-v2v uses. Needs nbdkit + the VDDK library
        (vddk_libdir) on the worker host; VDDK is downloaded separately from Broadcom.
  http  (fallback) HTTPS Range GETs of the base "-flat.vmdk" through the vCenter/ESXi /folder
        datastore browser. Allowed ONLY when mapping.flat_readable() proves the base file is the
        frozen snapshot image (no prior snapshot chain, flat backing). Slower; no extra software.

Usage:
    with VddkExport(cfg, server=..., user=..., password=..., thumbprint=..., vm_moref=..., snapshot_moref=...,
                    file_name="[ds1] vm/vm.vmdk") as reader: reader.read(0, 1 << 20)
    HttpFlatReader(session_get, url, size)

The VDDK password is passed to nbdkit as password=+FILE (a 0600 temp file), never on argv.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...core.errors import ConfigError, ProviderError, TransientProviderError
from ...core.interfaces import DiskReader
from ...core.nbd import NbdReader


def vddk_argv(*, nbdkit: str, sock: str, libdir: str, server: str, user: str, password_file: str, thumbprint: str,
              vm_moref: str, snapshot_moref: str, file_name: str, transports: str,
              extra: list[str] | None = None) -> list[str]:
    return [nbdkit, "--readonly", "--foreground", "--exit-with-parent", "--unix", sock, *(extra or []),
            "vddk", f"libdir={libdir}", f"server={server}", f"user={user}", f"password=+{password_file}",
            f"thumbprint={thumbprint}", f"vm=moref={vm_moref}", f"snapshot={snapshot_moref}",
            f"transports={transports}", f"file={file_name}"]


def vddk_problems(nbdkit: str, libdir: str | None) -> list[str]:
    p = []
    if not shutil.which(nbdkit):
        p.append(f"nbdkit not found ({nbdkit!r}); install nbdkit with the vddk plugin on this worker")
    if not libdir:
        p.append("vddk_libdir is not set (directory that contains lib64/libvixDiskLib.so*)")
    elif not list(Path(libdir).glob("lib64/libvixDiskLib.so*")):
        p.append(f"VDDK library not found under {libdir}/lib64 (download VDDK from Broadcom and unpack it there)")
    return p


class VddkExport(DiskReader):
    """Runs one nbdkit-vddk process for one disk and reads it over NBD. Close stops the process."""

    def __init__(self, *, nbdkit: str, libdir: str, server: str, user: str, password: str, thumbprint: str,
                 vm_moref: str, snapshot_moref: str, file_name: str, transports: str = "nbdssl:nbd",
                 start_timeout: float = 120.0, extra: list[str] | None = None):
        problems = vddk_problems(nbdkit, libdir)
        if problems:
            raise ConfigError("; ".join(problems))
        self._dir = tempfile.mkdtemp(prefix="vmigrate-vddk-")
        os.chmod(self._dir, 0o700)
        pw = Path(self._dir) / "pw"
        fd = os.open(pw, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(password)
        sock = str(Path(self._dir) / "nbd.sock")
        argv = vddk_argv(nbdkit=nbdkit, sock=sock, libdir=libdir, server=server, user=user, password_file=str(pw),
                         thumbprint=thumbprint, vm_moref=vm_moref, snapshot_moref=snapshot_moref, file_name=file_name,
                         transports=transports, extra=extra)
        self._proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + start_timeout
        while not Path(sock).exists():
            if self._proc.poll() is not None:
                err = (self._proc.stderr.read() if self._proc.stderr else "")[-2000:]
                self._cleanup()
                raise ProviderError(f"nbdkit-vddk exited ({self._proc.returncode}) for {file_name}: {err}")
            if time.monotonic() > deadline:
                self.close()
                raise TransientProviderError(f"nbdkit-vddk did not open its socket within {start_timeout}s")
            time.sleep(0.2)
        pw.unlink(missing_ok=True)   # nbdkit has read it
        self._nbd = NbdReader.connect_unix(sock)

    @property
    def size(self) -> int:
        return self._nbd.size

    def read(self, offset: int, length: int) -> bytes:
        return self._nbd.read(offset, length)

    def _cleanup(self) -> None:
        shutil.rmtree(self._dir, ignore_errors=True)

    def close(self) -> None:
        try:
            if hasattr(self, "_nbd"):
                self._nbd.close()
        finally:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._cleanup()


class HttpFlatReader(DiskReader):
    """Range reads of a flat VMDK through the datastore browser. `get(url, headers)` returns an
    object with .status_code, .content, .headers (a requests.Session.get partial in production)."""

    def __init__(self, get: Callable[..., Any], url: str, size: int, *, retries: int = 4):
        self._get, self.url, self._size, self.retries = get, url, size, retries

    @property
    def size(self) -> int:
        return self._size

    def read(self, offset: int, length: int) -> bytes:
        length = max(0, min(length, self._size - offset))
        if length == 0:
            return b""
        hdr = {"Range": f"bytes={offset}-{offset + length - 1}"}
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                r = self._get(self.url, headers=hdr)
                if r.status_code == 206 and len(r.content) == length:
                    return r.content
                if r.status_code == 200:
                    raise ProviderError("datastore server ignored the Range header (refusing to download whole disks)")
                if r.status_code in (401, 403, 404):
                    raise ProviderError(f"datastore read {r.status_code} for {self.url}")
                last = TransientProviderError(f"HTTP {r.status_code}, {len(r.content)} bytes for {hdr['Range']}")
            except ProviderError:
                raise
            except Exception as e:  # noqa: BLE001 - network hiccup
                last = e
            time.sleep(min(30, 2 ** attempt))
        raise TransientProviderError(f"datastore range read failed at {offset}: {last}")
