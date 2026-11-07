"""Purpose: the OpenVixDiskLib (VMware NFC) reader and the rct-service (Hyper-V) client, against
fakes of the documented library / REST APIs.

Usage: pytest tests/test_transports_extra.py
"""

from __future__ import annotations

import contextlib
import json

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.testclient import TestClient

from vmigrate.core.errors import ConfigError, ProviderError
from vmigrate.core.ranges import ByteRange
from vmigrate.providers.hyperv.rctservice import RctServiceClient, RctServiceReader
from vmigrate.providers.vmware.transports import OpenVixReader

DATA = bytes((i * 7) % 251 for i in range(3 * 1024 * 1024 + 300))


class FakeVix:
    VIXDISKLIB_FLAG_OPEN_READ_ONLY = 4
    VIXDISKLIB_SECTOR_SIZE = 512

    def __init__(self):
        self.opened, self.closed, self.reads = [], [], []
        outer = self

        class Handle:
            def __init__(self, vixdisklib_compatibility_version=None):
                assert vixdisklib_compatibility_version == "8.0"

            @contextlib.contextmanager
            def connect(self, **kw):
                assert kw["read_only"] is True and kw["vmx_spec"] == "moref=vm-42" and kw["thumbprint"] == "AA:BB"
                yield "conn"
                outer.closed.append("conn")

            @contextlib.contextmanager
            def open(self, conn, path, flags=0):
                assert flags == 4
                outer.opened.append(path)
                yield type("D", (), {"sector_size": 512})()
                outer.closed.append("disk")

            def read(self, disk, start, n, buf):
                outer.reads.append((start, n))
                chunk = DATA[start * 512:(start + n) * 512]
                buf.raw = chunk + bytes(n * 512 - len(chunk))

        self.VixDiskLibHandle = Handle

    @staticmethod
    def get_buffer(size):
        return type("B", (), {"raw": bytes(size)})()


def test_openvix_reader_sector_alignment_and_close():
    lib = FakeVix()
    r = OpenVixReader(server="vc", user="u", password="p", thumbprint="AA:BB", vm_moref="vm-42",
                      file_name="[ds1] vm/vm-000001.vmdk", size=len(DATA), lib=lib)
    assert r.read(100, 1000) == DATA[100:1100]
    assert r.read(0, len(DATA)) == DATA                                   # several 1 MiB NFC reads
    assert max(n for _, n in lib.reads) <= 2048
    assert r.read(len(DATA) - 5, 100) == DATA[-5:]
    r.close()
    assert lib.opened == ["[ds1] vm/vm-000001.vmdk"] and lib.closed == ["disk", "conn"]


def _rct_app(seen):
    app = FastAPI()

    @app.middleware("http")
    async def auth(request: Request, call_next):
        seen.append(request.url.path)
        if request.headers.get("auth_key") != "swordfish":
            return Response(status_code=401)
        return await call_next(request)

    @app.get("/vdisk/{path}/info")
    def info(path: str):
        return {"virtual_size": len(DATA), "parent_path": None, "virtual_storage_type": 3, "provider_sub_type": 2}

    @app.get("/vdisk/{path}/rct/{rid}/changes")
    def changes(path: str, rid: str):
        if rid != "rctX_1":
            return JSONResponse("RCT ID not found", status_code=404)
        return [{"offset": 0, "length": 4096}, {"offset": 1 << 20, "length": 65536}]

    @app.post("/vdisk/{path}/content")
    async def content(path: str, request: Request):
        rs = json.loads(await request.body())
        return Response(b"".join(DATA[r["offset"]:r["offset"] + r["length"]] for r in rs),
                        media_type="application/octet-stream")
    return app


def test_rct_service_client_and_reader():
    seen = []
    http = TestClient(_rct_app(seen), base_url="https://hv01:6677")
    c = RctServiceClient("https://hv01:6677", "swordfish", http=http)
    path = r"C:\ClusterStorage\Volume1\vm\disk0.vhdx"
    r = RctServiceReader(c, path)
    assert r.size == len(DATA) and r.read(5000, 70000) == DATA[5000:75000]
    assert c.changes(path, "rctX_1") == [ByteRange(0, 4096), ByteRange(1 << 20, 65536)]
    from urllib.parse import quote
    assert seen[0].count("/") == 3 and "%5C" in seen[0]                   # the Windows path is ONE encoded segment
    with pytest.raises(ProviderError, match="404"):
        c.changes(path, "nope")
    with pytest.raises(ProviderError, match="auth_key rejected"):
        RctServiceClient("https://hv01:6677", "wrong", http=http).info(path)
    with pytest.raises(ConfigError, match="https"):
        RctServiceClient("http://hv01:6677", "k")
