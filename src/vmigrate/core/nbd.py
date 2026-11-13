"""Purpose: a small, dependency-free NBD (Network Block Device) client, read-only, used to read
disks that a helper exports over NBD: nbdkit-vddk for VMware (the virt-v2v path), and later
qemu-nbd for Proxmox/OpenStack. Fixed-newstyle handshake + NBD_OPT_GO + simple replies.

Usage:
    with NbdReader.connect_unix("/run/vmigrate/disk0.sock") as r:     # or connect_tcp(host, port)
        r.size; r.read(offset, length)

Protocol notes (https://github.com/NetworkBlockDevice/nbd/blob/master/doc/proto.md):
    server: "NBDMAGIC" + IHAVEOPT + u16 handshake flags
    client: u32 flags (FIXED_NEWSTYLE | NO_ZEROES)
    client: IHAVEOPT + u32 NBD_OPT_GO + u32 len + (u32 namelen + name + u16 n_info=0)
    server: replies (magic 0x3e889045565a9) until NBD_REP_ACK; NBD_REP_INFO/EXPORT carries size
    transmission: request magic 0x25609513, simple reply magic 0x67446698
Reads larger than `max_request` are split; the export is opened read-only.
"""

from __future__ import annotations

import socket
import struct
from typing import Any

from .interfaces import DiskReader
from .ranges import ByteRange, normalize

NBDMAGIC = b"NBDMAGIC"
IHAVEOPT = 0x49484156454F5054
OPT_REPLY_MAGIC = 0x3E889045565A9
REQUEST_MAGIC = 0x25609513
SIMPLE_REPLY_MAGIC = 0x67446698
FLAG_FIXED_NEWSTYLE = 1 << 0
FLAG_NO_ZEROES = 1 << 1
OPT_GO = 7
OPT_STARTTLS = 5
REP_ACK = 1
REP_INFO = 3
REP_ERR_BIT = 1 << 31
INFO_EXPORT = 0
CMD_READ = 0
CMD_DISC = 2


class NbdError(IOError):
    pass


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        k = sock.recv_into(view[got:], n - got)
        if k == 0:
            raise NbdError(f"connection closed after {got}/{n} bytes")
        got += k
    return bytes(buf)


class NbdReader(DiskReader):
    """`tls`: an ssl.SSLContext (+ `server_hostname`) upgrades the connection with NBD_OPT_STARTTLS
    before the export is opened (XenServer / XCP-ng "nbd" networks require it)."""

    def __init__(self, sock: socket.socket, export: str = "", *, max_request: int = 8 << 20, tls: Any = None,
                 server_hostname: str | None = None):
        self.sock = sock
        self.max_request = max_request
        self._handle = 0
        self._tls, self._sni = tls, server_hostname
        self._size = self._handshake(export)

    @classmethod
    def connect_unix(cls, path: str, export: str = "", timeout: float = 300.0, **kw) -> NbdReader:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(path)
        return cls(s, export, **kw)

    @classmethod
    def connect_tcp(cls, host: str, port: int, export: str = "", timeout: float = 300.0, **kw) -> NbdReader:
        s = socket.create_connection((host, port), timeout=timeout)
        return cls(s, export, **kw)

    # ------------------------------------------------------------------ handshake
    def _handshake(self, export: str) -> int:
        s = self.sock
        if _recv_exact(s, 8) != NBDMAGIC:
            raise NbdError("not an NBD server (bad magic)")
        (opt_magic,) = struct.unpack(">Q", _recv_exact(s, 8))
        if opt_magic != IHAVEOPT:
            raise NbdError("server does not speak newstyle NBD (oldstyle is not supported)")
        (hflags,) = struct.unpack(">H", _recv_exact(s, 2))
        if not hflags & FLAG_FIXED_NEWSTYLE:
            raise NbdError("server does not support fixed newstyle negotiation")
        s.sendall(struct.pack(">I", FLAG_FIXED_NEWSTYLE | (hflags & FLAG_NO_ZEROES)))
        if self._tls is not None:
            s.sendall(struct.pack(">QII", IHAVEOPT, OPT_STARTTLS, 0))
            magic, opt, rtype, rlen = struct.unpack(">QIII", _recv_exact(s, 20))
            payload = _recv_exact(s, rlen) if rlen else b""
            if magic != OPT_REPLY_MAGIC or opt != OPT_STARTTLS or rtype != REP_ACK:
                raise NbdError(f"server refused STARTTLS (reply {rtype:#x} {payload[4:]!r})")
            s = self.sock = self._tls.wrap_socket(s, server_hostname=self._sni)
        name = export.encode()
        data = struct.pack(">I", len(name)) + name + struct.pack(">H", 0)
        s.sendall(struct.pack(">QII", IHAVEOPT, OPT_GO, len(data)) + data)
        size: int | None = None
        while True:
            magic, opt, rtype, rlen = struct.unpack(">QIII", _recv_exact(s, 20))
            payload = _recv_exact(s, rlen) if rlen else b""
            if magic != OPT_REPLY_MAGIC or opt != OPT_GO:
                raise NbdError(f"unexpected option reply (magic={magic:#x}, opt={opt})")
            if rtype & REP_ERR_BIT:
                raise NbdError(f"server refused export {export!r}: error {rtype & ~REP_ERR_BIT} {payload[4:]!r}")
            if rtype == REP_INFO and len(payload) >= 12:
                (itype,) = struct.unpack(">H", payload[:2])
                if itype == INFO_EXPORT:
                    size, _tflags = struct.unpack(">QH", payload[2:12])
            elif rtype == REP_ACK:
                break
        if size is None:
            raise NbdError("server did not report the export size")
        return size

    # ------------------------------------------------------------------ DiskReader
    @property
    def size(self) -> int:
        return self._size

    def _read_one(self, offset: int, length: int) -> bytes:
        self._handle += 1
        h = self._handle
        self.sock.sendall(struct.pack(">IHHQQI", REQUEST_MAGIC, 0, CMD_READ, h, offset, length))
        magic, err, rh = struct.unpack(">IIQ", _recv_exact(self.sock, 16))
        if magic != SIMPLE_REPLY_MAGIC or rh != h:
            raise NbdError(f"bad reply (magic={magic:#x}, handle={rh} != {h})")
        if err:
            raise NbdError(f"read error {err} at offset {offset} (+{length})")
        return _recv_exact(self.sock, length)

    def read(self, offset: int, length: int) -> bytes:
        length = max(0, min(length, self._size - offset))
        parts = []
        pos = offset
        while pos < offset + length:
            n = min(self.max_request, offset + length - pos)
            parts.append(self._read_one(pos, n))
            pos += n
        return b"".join(parts)

    def close(self) -> None:
        try:
            self.sock.sendall(struct.pack(">IHHQQI", REQUEST_MAGIC, 0, CMD_DISC, 0, 0, 0))
        except OSError:
            pass
        finally:
            self.sock.close()


# ---------------------------------------------------------------------- block status (extents)
OPT_STRUCTURED_REPLY = 8
OPT_SET_META_CONTEXT = 10
REP_META_CONTEXT = 4
CMD_BLOCK_STATUS = 7
STRUCTURED_REPLY_MAGIC = 0x668E33EF
CHUNK_BLOCK_STATUS = 5
STATE_HOLE = 1                  # base:allocation: bit 0 = hole (not allocated)
STATE_DIRTY = 1                 # qemu:dirty-bitmap:*: bit 0 = dirty (changed since the checkpoint)


class NbdStatus:
    """Second, metadata-only connection to an export: NBD_CMD_BLOCK_STATUS for one meta context,
    e.g. "base:allocation" (which ranges hold data) or "qemu:dirty-bitmap:backup-vda" (which ranges
    changed since a libvirt checkpoint). Reads stay on NbdReader.

    Usage:
        with NbdStatus.connect_unix(sock, "vda", "qemu:dirty-bitmap:backup-vda") as s:
            dirty = s.ranges(lambda flags: flags & STATE_DIRTY)     # [ByteRange]
    """

    def __init__(self, sock: socket.socket, export: str, context: str):
        self.sock, self.context = sock, context
        self._handle = 0
        self._ctx_id, self._size = self._handshake(export, context)

    @classmethod
    def connect_unix(cls, path: str, export: str, context: str, timeout: float = 300.0) -> NbdStatus:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(path)
        return cls(s, export, context)

    @classmethod
    def connect_tcp(cls, host: str, port: int, export: str, context: str, timeout: float = 300.0) -> NbdStatus:
        return cls(socket.create_connection((host, port), timeout=timeout), export, context)

    def _opt(self, opt: int, data: bytes) -> list[tuple[int, bytes]]:
        self.sock.sendall(struct.pack(">QII", IHAVEOPT, opt, len(data)) + data)
        out = []
        while True:
            magic, o, rtype, rlen = struct.unpack(">QIII", _recv_exact(self.sock, 20))
            payload = _recv_exact(self.sock, rlen) if rlen else b""
            if magic != OPT_REPLY_MAGIC or o != opt:
                raise NbdError(f"unexpected option reply (magic={magic:#x}, opt={o})")
            if rtype & REP_ERR_BIT:
                raise NbdError(f"server refused option {opt}: error {rtype & ~REP_ERR_BIT} {payload[4:]!r}")
            if rtype == REP_ACK:
                return out
            out.append((rtype, payload))

    def _handshake(self, export: str, context: str) -> tuple[int, int]:
        s = self.sock
        if _recv_exact(s, 8) != NBDMAGIC or struct.unpack(">Q", _recv_exact(s, 8))[0] != IHAVEOPT:
            raise NbdError("not a newstyle NBD server")
        (hflags,) = struct.unpack(">H", _recv_exact(s, 2))
        s.sendall(struct.pack(">I", FLAG_FIXED_NEWSTYLE | (hflags & FLAG_NO_ZEROES)))
        self._opt(OPT_STRUCTURED_REPLY, b"")
        name, q = export.encode(), context.encode()
        ctx = None
        for rtype, payload in self._opt(OPT_SET_META_CONTEXT, struct.pack(">I", len(name)) + name +
                                        struct.pack(">II", 1, len(q)) + q):
            if rtype == REP_META_CONTEXT and payload[4:] == q:
                ctx = struct.unpack(">I", payload[:4])[0]
        if ctx is None:
            raise NbdError(f"export {export!r} does not offer meta context {context!r}")
        size = None
        for rtype, payload in self._opt(OPT_GO, struct.pack(">I", len(name)) + name + struct.pack(">H", 0)):
            if rtype == REP_INFO and len(payload) >= 12 and struct.unpack(">H", payload[:2])[0] == INFO_EXPORT:
                size = struct.unpack(">Q", payload[2:10])[0]
        if size is None:
            raise NbdError("server did not report the export size")
        return ctx, size

    @property
    def size(self) -> int:
        return self._size

    def extents(self, offset: int, length: int) -> list[tuple[int, int, int]]:
        """[(offset, length, flags)] covering offset..offset+length (servers may answer in parts)."""
        out: list[tuple[int, int, int]] = []
        pos, end = offset, min(offset + length, self._size)
        while pos < end:
            self._handle += 1
            h = self._handle
            self.sock.sendall(struct.pack(">IHHQQI", REQUEST_MAGIC, 0, CMD_BLOCK_STATUS, h, pos, min(end - pos, 1 << 30)))
            got = 0
            while True:
                magic, flags, ctype, rh, ln = struct.unpack(">IHHQI", _recv_exact(self.sock, 20))
                payload = _recv_exact(self.sock, ln) if ln else b""
                if magic != STRUCTURED_REPLY_MAGIC or rh != h:
                    raise NbdError(f"bad structured reply (magic={magic:#x}, handle={rh} != {h})")
                if ctype & 0x8000:
                    err = struct.unpack(">I", payload[:4])[0] if len(payload) >= 4 else -1
                    raise NbdError(f"block status error {err} at {pos}: {payload[6:]!r}")
                if ctype == CHUNK_BLOCK_STATUS and struct.unpack(">I", payload[:4])[0] == self._ctx_id:
                    for i in range(4, len(payload) - 7, 8):
                        n, st = struct.unpack(">II", payload[i:i + 8])
                        n = min(n, end - (pos + got))
                        if n > 0:
                            out.append((pos + got, n, st))
                            got += n
                if flags & 1:
                    break
            if got == 0:
                raise NbdError(f"block status made no progress at {pos}")
            pos += got
        return out

    def ranges(self, want: Any) -> list[ByteRange]:
        """Merged ranges whose flags satisfy `want(flags)`."""
        return normalize(ByteRange(o, n) for o, n, f in self.extents(0, self._size) if want(f))

    def close(self) -> None:
        try:
            self.sock.sendall(struct.pack(">IHHQQI", REQUEST_MAGIC, 0, CMD_DISC, 0, 0, 0))
        except OSError:
            pass
        finally:
            self.sock.close()

    def __enter__(self) -> NbdStatus:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
