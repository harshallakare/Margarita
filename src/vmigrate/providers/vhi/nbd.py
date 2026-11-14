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
import queue
import shlex
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from ...core.errors import ProviderError, TransientProviderError
from ...core.interfaces import DiskReader, DiskWriter
from . import nbd_agent as A

MAX_REQ = 8 << 20           # NBD request size; up to `window` of them in flight

_AGENT_SRC = Path(A.__file__).read_text()


def agent_command(device: str, *, writable: bool, python: str = "python3", sudo: bool = True) -> str:
    """Remote shell command that serves `device` over NBD on stdin/stdout."""
    cmd = f"{python} -c {shlex.quote(_AGENT_SRC)} {shlex.quote(device)} {'rw' if writable else 'ro'}"
    return f"sudo -n {cmd}" if sudo else cmd


def _write_all(f: Any, data: Any) -> None:
    """write() on a raw pipe may write less than asked (e.g. interrupted); keep going."""
    view = memoryview(data)
    while view:
        n = f.write(view)
        if n is None:                                 # non-blocking raw stream with a full buffer
            time.sleep(0.001)
            continue
        view = view[n:]


def _grow_pipe(f: Any, size: int = 1 << 20) -> None:
    """Linux: a 1 MiB pipe to ssh instead of 64 KiB, so the sender rarely waits on ssh (best effort)."""
    import fcntl
    setsz = getattr(fcntl, "F_SETPIPE_SZ", None)
    if setsz is None or f is None:
        return
    try:
        fcntl.fcntl(f.fileno(), setsz, size)
    except OSError:
        pass


class NbdError(ProviderError):
    pass


class NbdConnectionLost(NbdError, TransientProviderError):
    """The SSH/NBD stream to the helper broke (network cut, helper rebooted). Retryable: the copy
    resumes from its durable watermark over a fresh connection."""


class _Pending:
    __slots__ = ("ctype", "length", "done", "err", "data", "wait")

    def __init__(self, ctype: int, length: int, wait: bool):
        self.ctype, self.length, self.wait = ctype, length, wait
        self.done = threading.Event()
        self.err: BaseException | None = None
        self.data = b""


class NbdClient:
    """NBD client with pipelined requests: up to `window` commands in flight, replies matched by
    handle on a receiver thread. Writes do not wait for their reply (the round trip to the helper
    used to stall every 32 MiB); errors from them surface on the next call or at flush/close.
    Requests go out on a sender thread, in submission order, so a caller never blocks on the pipe
    (seen live: the copy thread waited for SSH to drain each 64 MiB chunk before it could checksum the
    next one); payloads are sent as views of the caller's buffer, never copied."""

    def __init__(self, rfile: IO[bytes], wfile: IO[bytes], closer: Callable[[], None] | None = None,
                 *, window: int = 16):
        self.r, self.w = rfile, wfile
        self._closer = closer
        self._handle = 0
        self._lock = threading.Lock()          # serialises request sends
        self._plock = threading.Lock()         # guards _pending
        self._pending: dict[int, _Pending] = {}
        self._slots = threading.Semaphore(window)
        self._err: BaseException | None = None
        self._closing = False
        self.size, self.flags = self._handshake()
        self._rx = threading.Thread(target=self._recv_loop, name="nbd-rx", daemon=True)
        self._rx.start()
        self._sendq: queue.Queue = queue.Queue(maxsize=window + 4)
        self._tx = threading.Thread(target=self._send_loop, name="nbd-tx", daemon=True)
        self._tx.start()

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
        _grow_pipe(proc.stdin)
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
            raise NbdConnectionLost(f"helper NBD agent did not start: {e}; "
                                    f"{err.decode(errors='replace').strip()[:800]}") from e

    def _read(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            b = self.r.read(n - len(buf))
            if not b:
                raise EOFError("NBD connection closed")
            buf += b
        return bytes(buf)

    def _send(self, data: bytes) -> None:
        _write_all(self.w, data)
        self.w.flush()

    def _send_loop(self) -> None:
        try:
            while True:
                item = self._sendq.get()
                if item is None:
                    return
                header, payload = item
                _write_all(self.w, header)
                if payload:
                    _write_all(self.w, payload)
                if self._sendq.empty():
                    self.w.flush()
        except BaseException as e:  # noqa: BLE001 - fail every waiter, never hang
            if not self._closing:
                self._err = self._err or NbdConnectionLost(f"NBD send failed: {e}")
            with self._plock:
                left, self._pending = list(self._pending.values()), {}
            for pend in left:
                pend.err = self._err or NbdConnectionLost("NBD connection closed")
                pend.done.set()
                self._slots.release()
            while True:                               # unblock submitters waiting on a full queue
                try:
                    if self._sendq.get_nowait() is None:
                        return
                except queue.Empty:
                    return

    def _enqueue(self, item: tuple[bytes, Any] | None) -> None:
        while True:
            self._check()
            if not self._tx.is_alive():
                raise NbdConnectionLost("NBD connection closed (sender stopped)")
            try:
                self._sendq.put(item, timeout=1)
                return
            except queue.Full:
                continue

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

    # ------------------------------------------------------------------ pipelining
    def _recv_loop(self) -> None:
        try:
            while True:
                magic, err, handle = struct.unpack(">IIQ", self._read(16))
                with self._plock:
                    pend = self._pending.pop(handle, None)
                if magic != A.SIMPLE_REPLY or pend is None:
                    raise NbdError("bad NBD reply (magic or unknown handle)")
                if err:
                    pend.err = NbdError(f"NBD command {pend.ctype} failed: errno {err}")
                    if not pend.wait:                       # nobody waits on a pipelined write
                        self._err = self._err or pend.err
                elif pend.ctype == A.CMD_READ:
                    pend.data = self._read(pend.length)
                pend.done.set()
                self._slots.release()
        except BaseException as e:  # noqa: BLE001 - fail every waiter, never hang
            if not self._closing:
                self._err = self._err or (e if isinstance(e, NbdError) else NbdConnectionLost(f"NBD connection lost: {e}"))
            with self._plock:
                left, self._pending = list(self._pending.values()), {}
            for p in left:
                p.err = self._err or NbdConnectionLost("NBD connection closed")
                p.done.set()
                self._slots.release()

    def _check(self) -> None:
        if self._err is not None:
            raise self._err

    def _submit(self, ctype: int, off: int, ln: int, data: bytes = b"", flags: int = 0, *, wait: bool = True) -> bytes:
        self._check()
        while not self._slots.acquire(timeout=1):
            self._check()
            if not self._rx.is_alive():
                raise NbdError("NBD connection closed")
        pend = _Pending(ctype, ln, wait)
        with self._lock:                              # handle order == queue order == wire order
            self._handle += 1
            h = self._handle
            with self._plock:
                self._pending[h] = pend
            try:
                self._enqueue((struct.pack(">IHHQQI", A.REQ_MAGIC, flags, ctype, h, off, ln), data))
            except BaseException:
                with self._plock:
                    self._pending.pop(h, None)
                self._slots.release()
                raise
        if not wait:
            return b""
        pend.done.wait()
        if pend.err is not None:
            raise pend.err
        return pend.data

    def _drain(self) -> None:
        """Wait until every in-flight request has been answered."""
        while True:
            with self._plock:
                left = list(self._pending.values())
            if not left:
                break
            for p in left:
                p.done.wait()
        self._check()

    # ------------------------------------------------------------------ commands
    def pread(self, off: int, ln: int) -> bytes:
        out = []
        while ln > 0:
            n = min(ln, MAX_REQ)
            out.append(self._submit(A.CMD_READ, off, n))
            off, ln = off + n, ln - n
        return b"".join(out)

    def pwrite(self, off: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            n = min(len(view), MAX_REQ)
            self._submit(A.CMD_WRITE, off, n, view[:n], wait=False)     # sent from the caller's buffer
            off, view = off + n, view[n:]

    def write_zeroes(self, off: int, ln: int) -> None:
        while ln > 0:
            n = min(ln, 1 << 30)
            self._submit(A.CMD_WRITE_ZEROES, off, n, wait=False)
            off, ln = off + n, ln - n

    def flush(self) -> None:
        self._drain()                          # every earlier write acknowledged first
        self._submit(A.CMD_FLUSH, 0, 0)

    def close(self) -> None:
        try:
            self._drain()
        finally:
            self._closing = True                      # the agent's exit after DISC is not an error
            try:
                with self._lock:
                    self._enqueue((struct.pack(">IHHQQI", A.REQ_MAGIC, 0, A.CMD_DISC, 0, 0, 0), b""))
                    self._enqueue(None)
            except (OSError, ValueError, NbdError):
                pass
            self._tx.join(timeout=60)
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
