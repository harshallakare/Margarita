"""Purpose: read a physical server's block devices over ONE long-lived SSH pipe per disk.

Usage:
    r = PipeReader(spawn_argv, disk_map)    # spawn_argv: ssh ... (or [] to run locally in tests)
    r.read(offset, length); r.close()

A tiny Python program (READER, stdlib only, Python >= 3.6) runs on the server under sudo. It
serves fixed-size requests on stdin/stdout:
    request : 16 bytes  >QQ (offset, length)
    reply   : 1 byte kind + >Q n  then n bytes   kind Z = n zero bytes (no payload)
                                                     C = zlib payload, R = raw payload, E = error text
The disk map lets one image be composed from several sources (LVM snapshot mode):
    {"base": "/dev/sda", "size": N, "overlays": [[disk_off, length, path, src_off], ...]}
Overlay byte ranges are read from `path` instead of the base device. Nothing is ever written.
"""

from __future__ import annotations

import base64
import json
import struct
import subprocess
import threading
import zlib
from typing import Any

from ...core.errors import ProviderError, TransientProviderError
from ...core.interfaces import DiskReader

READER = r'''
import os, sys, struct, zlib, json, base64
m = json.loads(base64.b64decode(sys.argv[2]).decode())
size = int(m["size"])
base = os.open(m["base"], os.O_RDONLY)
fds = {}
def fd(p):
    if p not in fds:
        fds[p] = os.open(p, os.O_RDONLY)
    return fds[p]
ov = sorted((int(a), int(n), p, int(s)) for a, n, p, s in m.get("overlays", []))
def rd(off, n):
    out = bytearray(); end = min(off + n, size)
    while off < end:
        seg = None
        for a, l, p, s in ov:
            if a <= off < a + l:
                seg = (a, l, p, s); break
        if seg:
            a, l, p, s = seg; k = min(end, a + l) - off; b = os.pread(fd(p), k, s + (off - a))
        else:
            nxt = min([a for a, l, p, s in ov if a > off] + [end]); k = nxt - off; b = os.pread(base, k, off)
        if len(b) != k:
            raise IOError("short read at %d (%d of %d)" % (off, len(b), k))
        out += b; off += k
    return bytes(out)
i = sys.stdin.buffer; o = sys.stdout.buffer
o.write(b"MGR1" + struct.pack(">Q", size)); o.flush()
while True:
    h = i.read(16)
    if len(h) < 16:
        break
    off, n = struct.unpack(">QQ", h)
    try:
        b = rd(off, n)
    except Exception as e:
        t = str(e).encode(); o.write(b"E" + struct.pack(">Q", len(t)) + t); o.flush(); continue
    if b.count(0) == len(b):
        o.write(b"Z" + struct.pack(">Q", len(b)))
    else:
        z = zlib.compress(b, 1)
        if len(z) < len(b) * 0.9:
            o.write(b"C" + struct.pack(">Q", len(z)) + z)
        else:
            o.write(b"R" + struct.pack(">Q", len(b)) + b)
    o.flush()
'''

BOOT = "import base64,sys;exec(base64.b64decode(sys.argv[1]).decode())"


def remote_command(disk_map: dict[str, Any], *, python: str = "python3") -> list[str]:
    prog = base64.b64encode(READER.encode()).decode()
    m = base64.b64encode(json.dumps(disk_map).encode()).decode()
    return [python, "-c", BOOT, prog, m]


class PipeReader(DiskReader):
    def __init__(self, spawn: Any, disk_map: dict[str, Any], *, python: str = "python3"):
        """spawn(argv) -> Popen with binary stdin/stdout (SshNode.spawn or LocalShell-like)."""
        self._p: subprocess.Popen = spawn(remote_command(disk_map, python=python))
        self._lock = threading.Lock()
        hello = self._exact(12)
        if hello[:4] != b"MGR1":
            err = (self._p.stderr.read() if self._p.stderr else b"")[-800:]
            raise ProviderError(f"remote disk reader did not start: {hello!r} {err.decode(errors='replace')}")
        self._size = struct.unpack(">Q", hello[4:])[0]

    def _exact(self, n: int) -> bytes:
        assert self._p.stdout is not None
        buf = bytearray()
        while len(buf) < n:
            b = self._p.stdout.read(n - len(buf))
            if not b:
                raise TransientProviderError(f"remote disk reader closed the stream (exit {self._p.poll()})")
            buf += b
        return bytes(buf)

    @property
    def size(self) -> int:
        return self._size

    def read(self, offset: int, length: int) -> bytes:
        length = max(0, min(length, self._size - offset))
        if length == 0:
            return b""
        with self._lock:
            assert self._p.stdin is not None
            try:
                self._p.stdin.write(struct.pack(">QQ", offset, length))
                self._p.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise TransientProviderError(f"remote disk reader: {e}") from e
            hdr = self._exact(9)
            kind, n = hdr[:1], struct.unpack(">Q", hdr[1:])[0]
            if kind == b"Z":
                return bytes(n)
            payload = self._exact(n)
        if kind == b"E":
            raise ProviderError(f"remote read at {offset}: {payload.decode(errors='replace')}")
        data = zlib.decompress(payload) if kind == b"C" else payload
        if len(data) != length:
            raise TransientProviderError(f"remote read at {offset}: got {len(data)} of {length} bytes")
        return data

    def close(self) -> None:
        p = self._p
        try:
            if p.stdin:
                p.stdin.close()
            p.wait(timeout=15)
        except Exception:  # noqa: BLE001
            p.kill()
