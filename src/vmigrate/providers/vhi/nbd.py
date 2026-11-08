"""Purpose: worker side of the helper-VM write path: an NBD client speaking to nbd_agent.py through a
pipe (normally an SSH session to the helper VM), DiskWriter/DiskReader on top of it, and a local
Unix-socket bridge so libguestfs (OSMorph) can open the same remote volume as `nbd://?socket=...`.

Usage:
    proc = transport.popen(agent_command(device, writable=True))
    with NbdDiskWriter(NbdClient.over_process(proc), size) as w: w.write(0, data)
    bridge = UnixBridge(sock_path, lambda: transport.popen(agent_command(device, writable=True)))
    guestfish --format=raw -a "nbd://?socket=<sock_path>"   # one SSH session per connection

The agent source is sent inline (`python3 -c`), so nothing is installed on the helper.
"""

from __future__ import annotations

import os
import shlex
import socket
import struct
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import IO

from ...core.errors import ProviderError
from ...core.interfaces import DiskReader, DiskWriter
from . import nbd_agent as A

MAX_REQ = 32 << 20          # split large writes/reads into NBD requests of at most this size

_AGENT_SRC = Path(A.__file__).read_text()


def agent_command(device: str, *, writable: bool, python: str = "python3", sudo: bool = True) -> str:
    """Remote shell command that serves `device` over NBD on stdin/stdout."""
    cmd = f"{python} -c {shlex.quote(_AGENT_SRC)} {shlex.quote(device)} {'rw' if writable else 'ro'}"
    return f"sudo -n {cmd}" if sudo else cmd


class NbdError(ProviderError):
    pass


class NbdClient:
    def __init__(self, rfile: IO[bytes], wfile: IO[bytes], closer: Callable[[], None] | None = None):
        self.r, self.w = rfile, wfile
        self._closer = closer
        self._handle = 0
        self._lock = threading.Lock()
        self.size, self.flags = self._handshake()

    @classmethod
    def over_process(cls, proc: subprocess.Popen) -> NbdClient:
        assert proc.stdin is not None and proc.stdout is not None

        def close() -> None:
            for f in (proc.stdin, proc.stdout):
                try:
                    f.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
        try:
            return cls(proc.stdout, proc.stdin, close)
        except (EOFError, OSError, ValueError) as e:
            err = b""
            if proc.stderr is not None:
                try:
                    proc.kill()
                    err = proc.stderr.read() or b""
                except OSError:
                    pass
            raise NbdError(f"helper NBD agent did not start: {e}; {err.decode(errors='replace').strip()[:800]}") from e

    def _read(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            b = self.r.read(n - len(buf))
            if not b:
                raise EOFError("NBD connection closed")
            buf += b
        return bytes(buf)

    def _send(self, data: bytes) -> None:
        self.w.write(data)
        self.w.flush()

    def _handshake(self) -> tuple[int, int]:
        hello = self._read(18)
        if hello[:8] != A.NBDMAGIC or struct.unpack(">Q", hello[8:16])[0] != A.IHAVEOPT:
            raise NbdError("not an NBD server (bad greeting)")
        self._send(struct.pack(">I", A.FLAG_FIXED_NEWSTYLE | A.FLAG_NO_ZEROES))
        self._send(struct.pack(">QII", A.IHAVEOPT, A.OPT_GO, 6) + struct.pack(">IH", 0, 0))
        size = flags = None
        while True:
            magic, _opt, rtype, ln = struct.unpack(">QIII", self._read(20))
            if magic != A.REPLY_MAGIC:
                raise NbdError("bad option reply magic")
            data = self._read(ln)
            if rtype == A.REP_INFO and struct.unpack(">H", data[:2])[0] == A.INFO_EXPORT:
                size, flags = struct.unpack(">QH", data[2:12])
            elif rtype == A.REP_ACK:
                break
            elif rtype & 0x80000000:
                raise NbdError(f"NBD server refused OPT_GO (reply type {rtype:#x})")
        if size is None:
            raise NbdError("NBD server sent no export size")
        return size, flags or 0

    def _cmd(self, ctype: int, off: int, ln: int, data: bytes = b"", flags: int = 0) -> bytes:
        with self._lock:
            self._handle += 1
            h = self._handle
            self._send(struct.pack(">IHHQQI", A.REQ_MAGIC, flags, ctype, h, off, ln) + data)
            magic, err, handle = struct.unpack(">IIQ", self._read(16))
            if magic != A.SIMPLE_REPLY or handle != h:
                raise NbdError("bad NBD reply")
            if err:
                raise NbdError(f"NBD command {ctype} at {off}+{ln} failed: errno {err}")
            return self._read(ln) if ctype == A.CMD_READ else b""

    def pread(self, off: int, ln: int) -> bytes:
        out = []
        while ln > 0:
            n = min(ln, MAX_REQ)
            out.append(self._cmd(A.CMD_READ, off, n))
            off, ln = off + n, ln - n
        return b"".join(out)

    def pwrite(self, off: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            n = min(len(view), MAX_REQ)
            self._cmd(A.CMD_WRITE, off, n, bytes(view[:n]))
            off, view = off + n, view[n:]

    def write_zeroes(self, off: int, ln: int) -> None:
        while ln > 0:
            n = min(ln, 1 << 30)
            self._cmd(A.CMD_WRITE_ZEROES, off, n)
            off, ln = off + n, ln - n

    def flush(self) -> None:
        self._cmd(A.CMD_FLUSH, 0, 0)

    def close(self) -> None:
        try:
            with self._lock:
                self._send(struct.pack(">IHHQQI", A.REQ_MAGIC, 0, A.CMD_DISC, 0, 0, 0))
        except (OSError, ValueError):
            pass
        if self._closer:
            self._closer()


class NbdDiskWriter(DiskWriter):
    """Writes straight into the attached volume; no local staging (staged_path is None)."""

    def __init__(self, client: NbdClient, size: int, *, prezeroed: bool):
        if client.size < size:
            client.close()
            raise NbdError(f"helper device is {client.size} bytes, smaller than the volume ({size})")
        self.c, self._size = client, size
        self.prezeroed = prezeroed
        self._closed = False

    @property
    def size(self) -> int:
        return self._size

    def write(self, offset: int, buf: bytes) -> None:
        if offset + len(buf) > self._size:
            raise ValueError(f"write past end: {offset}+{len(buf)} > {self._size}")
        self.c.pwrite(offset, buf)

    def write_zeroes(self, offset: int, length: int) -> None:
        if not self.prezeroed:
            self.c.write_zeroes(offset, length)     # one small request instead of shipping zeros

    def flush(self) -> None:
        if not self._closed:
            self.c.flush()

    def close(self) -> None:
        if not self._closed:
            self.c.flush()
            self._closed = True
            self.c.close()


class NbdDiskReader(DiskReader):
    def __init__(self, client: NbdClient, size: int):
        self.c, self._size = client, size

    @property
    def size(self) -> int:
        return self._size

    def read(self, offset: int, length: int) -> bytes:
        length = max(0, min(length, self._size - offset))
        return self.c.pread(offset, length) if length else b""

    def close(self) -> None:
        self.c.close()


class UnixBridge:
    """Listens on a local Unix socket; each connection gets its own agent session (spawned by
    `spawn`, e.g. an SSH process) and bytes are pumped both ways. Used by guestfish/virt-v2v."""

    def __init__(self, path: Path, spawn: Callable[[], subprocess.Popen]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        self._spawn = spawn
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self._srv.listen(4)
        self._stop = threading.Event()
        self._procs: list[subprocess.Popen] = []
        self._t = threading.Thread(target=self._accept, name=f"nbd-bridge-{self.path.name}", daemon=True)
        self._t.start()

    @property
    def uri(self) -> str:
        return f"nbd://?socket={self.path}"

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            try:
                proc = self._spawn()
            except Exception:  # noqa: BLE001 - refuse this client; the next may succeed
                conn.close()
                continue
            self._procs.append(proc)
            threading.Thread(target=self._pump_in, args=(conn, proc), daemon=True).start()
            threading.Thread(target=self._pump_out, args=(conn, proc), daemon=True).start()

    @staticmethod
    def _pump_in(conn: socket.socket, proc: subprocess.Popen) -> None:
        try:
            while data := conn.recv(1 << 20):
                proc.stdin.write(data)       # type: ignore[union-attr]
                proc.stdin.flush()           # type: ignore[union-attr]
        except OSError:
            pass
        finally:
            try:
                proc.stdin.close()           # type: ignore[union-attr]
            except OSError:
                pass

    @staticmethod
    def _pump_out(conn: socket.socket, proc: subprocess.Popen) -> None:
        try:
            out = proc.stdout
            read = getattr(out, "read1", None) or out.read   # type: ignore[union-attr]  # raw pipes: read() = one read
            while data := read(1 << 20):
                conn.sendall(data)
        except OSError:
            pass
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()

    def close(self) -> None:
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass
        for p in self._procs:
            if p.poll() is None:
                p.kill()
        self.path.unlink(missing_ok=True)
