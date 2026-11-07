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

from .interfaces import DiskReader

NBDMAGIC = b"NBDMAGIC"
IHAVEOPT = 0x49484156454F5054
OPT_REPLY_MAGIC = 0x3E889045565A9
REQUEST_MAGIC = 0x25609513
SIMPLE_REPLY_MAGIC = 0x67446698
FLAG_FIXED_NEWSTYLE = 1 << 0
FLAG_NO_ZEROES = 1 << 1
OPT_GO = 7
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
    def __init__(self, sock: socket.socket, export: str = "", *, max_request: int = 8 << 20):
        self.sock = sock
        self.max_request = max_request
        self._handle = 0
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
