"""Purpose: JSON codec for the export-agent protocol (provider call arguments and results) plus
request signing.

Usage:
    wire = encode(snapshot_ref); obj = decode(wire)
    hdr = sign(key, "POST", "/v1/call", body); verify(key, "POST", "/v1/call", body, hdr)

Only the reference types the SourceProvider contract uses cross the wire: VmRef, SnapshotRef,
CheckpointRef, VmSpec, DiskSpec, ByteRange (+ JSON scalars, lists, dicts). Anything else is
refused, so the agent never deserialises arbitrary objects.

Auth: each request carries `X-Vmigrate-Auth: <unix-ts>.<hex hmac>` where the HMAC (SHA-256) is
keyed by a value derived from the platform secret key (shared by every server/worker) over
"<ts>\\n<METHOD>\\n<path>\\n<sha256(body)>". Requests older than 5 minutes are rejected.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import asdict
from typing import Any

from ..core.interfaces import CheckpointRef, SnapshotRef, VmRef
from ..core.ranges import ByteRange
from ..ir import DiskBus, DiskSpec, VmSpec

MAX_SKEW = 300


def encode(o: Any) -> Any:
    if o is None or isinstance(o, (bool, int, float, str)):
        return o
    if isinstance(o, VmRef):
        return {"__t": "VmRef", "v": asdict(o)}
    if isinstance(o, SnapshotRef):
        return {"__t": "SnapshotRef", "v": {"id": o.id, "vm": asdict(o.vm), "app_consistent": o.app_consistent,
                                             "data": _plain(o.data)}}
    if isinstance(o, CheckpointRef):
        return {"__t": "CheckpointRef", "v": {"id": o.id, "data": _plain(o.data)}}
    if isinstance(o, VmSpec):
        return {"__t": "VmSpec", "v": o.to_dict()}
    if isinstance(o, DiskSpec):
        return {"__t": "DiskSpec", "v": asdict(o)}
    if isinstance(o, ByteRange):
        return {"__t": "ByteRange", "v": [o.offset, o.length]}
    if isinstance(o, (list, tuple)):
        return [encode(x) for x in o]
    if isinstance(o, dict):
        if "__t" in o:
            raise TypeError("dict key '__t' is reserved by the export codec")
        return {str(k): encode(v) for k, v in o.items()}
    raise TypeError(f"export codec: cannot send {type(o).__name__}")


def decode(o: Any) -> Any:
    if isinstance(o, list):
        return [decode(x) for x in o]
    if not isinstance(o, dict):
        return o
    t = o.get("__t")
    if t is None:
        return {k: decode(v) for k, v in o.items()}
    v = o["v"]
    if t == "VmRef":
        return VmRef(**v)
    if t == "SnapshotRef":
        return SnapshotRef(id=v["id"], vm=VmRef(**v["vm"]), app_consistent=bool(v["app_consistent"]),
                           data=v.get("data") or {})
    if t == "CheckpointRef":
        return CheckpointRef(id=v["id"], data=v.get("data") or {})
    if t == "VmSpec":
        return VmSpec.from_dict(v)
    if t == "DiskSpec":
        return DiskSpec(**{**v, "bus": DiskBus(v["bus"])})
    if t == "ByteRange":
        return ByteRange(int(v[0]), int(v[1]))
    raise TypeError(f"export codec: unknown type tag {t!r}")


def _plain(m: Any) -> Any:
    """Provider-private `data` mappings are JSON-safe by contract; normalise Mapping -> dict."""
    if isinstance(m, dict) or hasattr(m, "items"):
        return {str(k): _plain(v) for k, v in m.items()}
    if isinstance(m, (list, tuple)):
        return [_plain(x) for x in m]
    return m


# --------------------------------------------------------------------------- auth
def derive_key(secret_key: str) -> bytes:
    return hashlib.sha256(b"vmigrate-export-v1\0" + secret_key.encode()).digest()


def sign(key: bytes, method: str, path: str, body: bytes, *, now: float | None = None) -> str:
    ts = str(int(now if now is not None else time.time()))
    msg = f"{ts}\n{method.upper()}\n{path}\n{hashlib.sha256(body).hexdigest()}".encode()
    return f"{ts}.{hmac.new(key, msg, hashlib.sha256).hexdigest()}"


def verify(key: bytes, method: str, path: str, body: bytes, header: str | None, *, now: float | None = None) -> bool:
    if not header or "." not in header:
        return False
    ts, mac = header.split(".", 1)
    try:
        t = int(ts)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - t) > MAX_SKEW:
        return False
    want = sign(key, method, path, body, now=t).split(".", 1)[1]
    return hmac.compare_digest(want, mac)
