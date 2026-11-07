"""Purpose: minimal Prism REST client (v1 / v2.0 on Prism Element, v3 on Prism Central or PE).
`requests` is imported here only, so the core never depends on it.

Usage:
    c = PrismClient(pe_url="https://pe:9440", pc_url=None, username="admin", password="...", verify_tls=False)
    c.v3_post("/vms/list", {"kind": "vm", "length": 250, "offset": 0})
    c.v2_get("/vms/<uuid>", params={"include_vm_disk_config": "true"})

5xx / connection errors raise TransientProviderError (retried with backoff); 4xx raise ProviderError.
"""

from __future__ import annotations

from typing import Any

from ...core.errors import ProviderError, TransientProviderError
from ...core.retry import retry

V1 = "/PrismGateway/services/rest/v1"
V2 = "/PrismGateway/services/rest/v2.0"
V3 = "/api/nutanix/v3"


class PrismClient:
    def __init__(self, *, pe_url: str, pc_url: str | None, username: str, password: str,
                 verify_tls: bool | str = True, timeout: float = 60, attempts: int = 5):
        import requests  # local import: provider-private dependency

        self._requests = requests
        self.pe = pe_url.rstrip("/")
        self.pc = (pc_url or pe_url).rstrip("/")
        self.s = requests.Session()
        self.s.auth = (username, password)
        self.s.verify = verify_tls
        self.s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        self.timeout = timeout
        self.attempts = attempts

    def _req(self, method: str, url: str, **kw: Any) -> Any:
        def once() -> Any:
            try:
                r = self.s.request(method, url, timeout=self.timeout, **kw)
            except self._requests.RequestException as e:
                raise TransientProviderError(f"{method} {url}: {e}") from e
            if r.status_code >= 500 or r.status_code == 429:
                raise TransientProviderError(f"{method} {url}: HTTP {r.status_code} {r.text[:500]}")
            if r.status_code >= 400:
                raise ProviderError(f"{method} {url}: HTTP {r.status_code} {r.text[:1000]}")
            return r.json() if r.content else {}

        return retry(once, attempts=self.attempts, what=f"{method} {url}")

    # v1/v2 live on Prism Element; v3 on Prism Central (or PE when no PC is configured)
    def v1_get(self, path: str, **kw: Any) -> Any:
        return self._req("GET", self.pe + V1 + path, **kw)

    def v1_post(self, path: str, body: Any) -> Any:
        return self._req("POST", self.pe + V1 + path, json=body)

    def v2_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._req("GET", self.pe + V2 + path, params=params)

    def v2_post(self, path: str, body: Any) -> Any:
        return self._req("POST", self.pe + V2 + path, json=body)

    def v2_delete(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._req("DELETE", self.pe + V2 + path, params=params)

    def v3_get(self, path: str) -> Any:
        return self._req("GET", self.pc + V3 + path)

    def v3_post(self, path: str, body: Any) -> Any:
        return self._req("POST", self.pc + V3 + path, json=body)
