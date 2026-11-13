"""Purpose: XenAPI (XAPI) over JSON-RPC (https://<pool master>/jsonrpc, XenServer 7.3+ / XCP-ng
8.x), with no XenAPI.py dependency, plus the raw-VDI export stream.

Usage:
    x = XenApi("https://xen1.example.com", "root", password, ca_pem=..., http=None)
    x.call("VM.get_all_records")                     # session ref is prepended automatically
    x.export_url(vdi_uuid)                           # https://host/export_raw_vdi?... (session inside: memory only)
    x.logout()

A pool member answers HOST_IS_SLAVE with the master's address; the client follows it once.
Permissions: a pool-admin or VM-power-admin role (snapshots, VDI export, CBT).
NOT run against a live pool yet: tests use a fake XAPI.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from ...core.errors import ProviderError, TransientProviderError
from ..common.http import Http, default_http, tls_context


class XapiError(ProviderError):
    def __init__(self, code: str, details: list[Any]):
        super().__init__(f"XAPI {code}: {details}")
        self.code, self.details = code, details


class XenApi:
    def __init__(self, url: str, username: str, password: str, *, ca_pem: str | None = None, verify_tls: bool = True,
                 http: Http | None = None):
        self.url = url.rstrip("/") if url.startswith("http") else f"https://{url.rstrip('/')}"
        self.user, self.password = username, password
        self.ctx = tls_context(verify_tls, ca_pem)
        self.http = http or default_http(self.ctx)
        self._session: str | None = None
        self._id = 0

    def _rpc(self, method: str, params: list[Any]) -> Any:
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": self._id}).encode()
        try:
            st, _, raw = self.http("POST", self.url + "/jsonrpc", {"Content-Type": "application/json"}, body, 300)
        except OSError as e:
            raise TransientProviderError(f"XAPI {method}: {e}") from e
        if st != 200:
            raise (TransientProviderError if st >= 500 else ProviderError)(f"XAPI {method}: HTTP {st}")
        d = json.loads(raw)
        err = d.get("error")
        if err:
            data = err.get("data") or []
            code = str(data[0] if data else err.get("message"))
            raise XapiError(code, list(data[1:]) if data else [err.get("message")])
        return d.get("result")

    def login(self) -> str:
        if self._session is None:
            try:
                self._session = self._rpc("session.login_with_password", [self.user, self.password, "1.0", "vmigrate"])
            except XapiError as e:
                if e.code == "HOST_IS_SLAVE" and e.details:
                    self.url = f"https://{e.details[0]}"
                    self._session = self._rpc("session.login_with_password", [self.user, self.password, "1.0", "vmigrate"])
                elif e.code == "SESSION_AUTHENTICATION_FAILED":
                    raise PermissionError("XenServer login failed: check the user name and password") from e
                else:
                    raise
        return self._session

    def call(self, method: str, *args: Any) -> Any:
        try:
            return self._rpc(method, [self.login(), *args])
        except XapiError as e:
            if e.code != "SESSION_INVALID":
                raise
            self._session = None
            return self._rpc(method, [self.login(), *args])

    def logout(self) -> None:
        if self._session:
            try:
                self._rpc("session.logout", [self._session])
            except ProviderError:
                pass
            self._session = None

    def export_url(self, vdi_uuid: str, base_uuid: str | None = None) -> str:
        q = {"session_id": self.login(), "vdi": vdi_uuid, "format": "raw"}
        if base_uuid:
            q["base"] = base_uuid
        return f"{self.url}/export_raw_vdi?{urllib.parse.urlencode(q)}"
