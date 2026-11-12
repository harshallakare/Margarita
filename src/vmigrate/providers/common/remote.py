"""Purpose: expose a disk AS OF a snapshot to this worker over NBD, either on a remote host over
SSH (Proxmox nodes, Ceph hosts) or locally (worker with Ceph access): setup (activate LV / zfs
clone) -> qemu-nbd --read-only on a unix socket -> (SSH forwards the socket here) ->
core.nbd.NbdReader -> teardown on close.

Usage:
    shell = SshNode(host="10.0.0.11", user="root", key_pem=..., known_hosts="10.0.0.11 ssh-ed25519 AAAA...")
    shell = LocalShell()                                     # run qemu-nbd on this worker
    shell.run(["pvesm", "path", "local-lvm:vm-100-disk-0"])
    with NodeExport(shell, plan, tag="vmigrate-abc-scsi0") as reader: reader.read(0, 1 << 20)

Safety:
  * host keys are PINNED (known_hosts from config, StrictHostKeyChecking=yes) — no trust on first use
  * the private key is written to a 0600 temp file for the ssh process only, then removed
  * commands are argv lists; each argument is shell-quoted for the remote side (ssh always runs a
    remote shell), and every name comes from the PVE API and is validated by mapping.safe()
  * temporary objects (zfs clones, sockets, pid files) are named vmigrate-* and removed on close
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from ...core import cancel
from ...core.errors import ConfigError, ProviderError, TransientProviderError
from ...core.interfaces import DiskReader
from ...core.nbd import NbdReader

RUN_DIR = "/run/vmigrate"


class SshNode:
    def __init__(self, host: str, user: str, key_pem: str, known_hosts: str, *, port: int = 22, ssh: str = "ssh",
                 timeout: float = 600, who: str = "proxmox"):
        if not known_hosts.strip():
            raise ConfigError(f"{who}: ssh_known_hosts is required (pin the host key; e.g. output of "
                              "`ssh-keyscan -t ed25519 <host>` checked against the console)")
        if not key_pem.strip():
            raise ConfigError(f"{who}: ssh_private_key is required in the endpoint credentials")
        self.host, self.user, self.port, self.ssh, self.timeout = host, user, port, ssh, timeout
        self._dir = tempfile.mkdtemp(prefix="vmigrate-ssh-")
        os.chmod(self._dir, 0o700)
        self.key = Path(self._dir) / "id"
        fd = os.open(self.key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key_pem.strip() + "\n")
        self.kh = Path(self._dir) / "known_hosts"
        self.kh.write_text(known_hosts.strip() + "\n")

    def base_argv(self) -> list[str]:
        return [self.ssh, "-i", str(self.key), "-p", str(self.port), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                "-o", f"UserKnownHostsFile={self.kh}", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=20",
                "-o", "ServerAliveInterval=30", f"{self.user}@{self.host}"]

    def run(self, argv: list[str], *, check: bool = True, timeout: float | None = None) -> str:
        remote = " ".join(shlex.quote(a) for a in argv)
        if self.user != "root":
            remote = "sudo -n " + remote
        p = cancel.run([*self.base_argv(), "--", remote], capture_output=True, text=True,
                           timeout=timeout or self.timeout)
        if p.returncode == 255:
            raise TransientProviderError(f"ssh {self.host}: {p.stderr.strip()[-500:]}")
        if check and p.returncode != 0:
            raise ProviderError(f"{self.host}: `{argv[0]}` failed ({p.returncode}): {p.stderr.strip()[-1000:]}")
        return p.stdout

    def spawn(self, argv: list[str]) -> subprocess.Popen:
        """Long-lived remote process with binary stdin/stdout pipes (disk reader streams)."""
        remote = " ".join(shlex.quote(a) for a in argv)
        if self.user != "root":
            remote = "sudo -n " + remote
        return subprocess.Popen([*self.base_argv(), "--", remote], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)

    def forward(self, local_sock: str, remote_sock: str) -> subprocess.Popen:
        argv = [*self.base_argv()[:-1], "-N", "-o", "ExitOnForwardFailure=yes", "-o", "StreamLocalBindUnlink=yes",
                "-L", f"{local_sock}:{remote_sock}", self.base_argv()[-1]]
        return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

    def close(self) -> None:
        shutil.rmtree(self._dir, ignore_errors=True)


class LocalShell:
    """Runs the same argv lists on THIS worker (no SSH). Sockets live in a private temp dir."""

    local = True

    def __init__(self, timeout: float = 600):
        self.timeout = timeout
        self.run_dir = tempfile.mkdtemp(prefix="vmigrate-run-")
        os.chmod(self.run_dir, 0o700)

    def run(self, argv: list[str], *, check: bool = True, timeout: float | None = None) -> str:
        p = cancel.run(argv, capture_output=True, text=True, timeout=timeout or self.timeout)
        if check and p.returncode != 0:
            raise ProviderError(f"`{argv[0]}` failed ({p.returncode}): {p.stderr.strip()[-1000:]}")
        return p.stdout

    def close(self) -> None:
        shutil.rmtree(self.run_dir, ignore_errors=True)


def qemu_nbd_argv(sock: str, pid: str, target: str, fmt: str, extra: list[str]) -> list[str]:
    return ["qemu-nbd", "--fork", f"--pid-file={pid}", "--read-only", "--persistent", "--shared=1",
            f"--socket={sock}", f"--format={fmt}", "--cache=none", "--aio=threads", *extra, target]


class NodeExport(DiskReader):
    """One disk, as of one snapshot, served by qemu-nbd on the node and read here over NBD."""

    def __init__(self, shell: Any, plan: dict[str, Any], *, tag: str, start_timeout: float = 60):
        if not tag.startswith("vmigrate-"):
            raise ProviderError("export tags must start with vmigrate-")
        self.shell, self.plan, self.tag = shell, plan, tag
        self.local = bool(getattr(shell, "local", False))
        run_dir = getattr(shell, "run_dir", RUN_DIR)
        self.rsock, self.rpid = f"{run_dir}/{tag}.sock", f"{run_dir}/{tag}.pid"
        self._ldir = tempfile.mkdtemp(prefix="vmigrate-nbd-")
        os.chmod(self._ldir, 0o700)
        self.lsock = str(Path(self._ldir) / "nbd.sock")
        self._fwd: subprocess.Popen | None = None
        self._nbd: NbdReader | None = None
        self._set_up = False
        try:
            shell.run(["mkdir", "-p", "-m", "0700", run_dir])
            self._set_up = True     # before setup: a half-done setup (clone made, settle failed) is torn down
            for argv in plan["setup"]:
                shell.run(argv)
            shell.run(qemu_nbd_argv(self.rsock, self.rpid, plan["nbd_target"], plan["nbd_format"], plan["nbd_extra"]))
            if self.local:
                self.lsock = self.rsock       # qemu-nbd listens right here
            else:
                self._fwd = shell.forward(self.lsock, self.rsock)
            deadline = time.monotonic() + start_timeout
            while True:
                if self._fwd is not None and self._fwd.poll() is not None:
                    raise ProviderError(f"ssh socket forward exited: {(self._fwd.stderr.read() if self._fwd.stderr else '')[-500:]}")
                if Path(self.lsock).exists():
                    try:
                        self._nbd = NbdReader.connect_unix(self.lsock)
                        break
                    except OSError:
                        pass
                if time.monotonic() > deadline:
                    raise TransientProviderError(f"NBD export for {plan['nbd_target']} not reachable within {start_timeout}s")
                time.sleep(0.3)
        except Exception:
            self.close()
            raise

    @property
    def size(self) -> int:
        assert self._nbd is not None
        return self._nbd.size

    def read(self, offset: int, length: int) -> bytes:
        assert self._nbd is not None
        return self._nbd.read(offset, length)

    def close(self) -> None:
        if self._nbd is not None:
            self._nbd.close()
            self._nbd = None
        if self._fwd is not None and self._fwd.poll() is None:
            self._fwd.terminate()
            try:
                self._fwd.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._fwd.kill()
        self._fwd = None
        try:
            self.shell.run(["sh", "-c", f'[ -f {shlex.quote(self.rpid)} ] && kill "$(cat {shlex.quote(self.rpid)})"; '
                                         f'sleep 1; rm -f {shlex.quote(self.rpid)} {shlex.quote(self.rsock)}'],
                           check=False)
            if self._set_up:
                for argv in self.plan["teardown"]:
                    self.shell.run(argv, check=False)
        finally:
            shutil.rmtree(self._ldir, ignore_errors=True)
