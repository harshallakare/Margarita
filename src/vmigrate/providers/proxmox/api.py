"""Purpose: the narrow slice of the Proxmox VE REST API the source needs (plain `requests`, API-token
auth). Read-only except snapshot create/delete of snapshots named vmigrate-*.

Usage:
    api = PveApi("https://pve1:8006", "migrate@pve!vmigrate", token_secret, verify_tls=True)
    api.vms(); api.config("pve1", 100); api.storage("local-lvm"); api.content("pve1", "local-lvm", 100)
    api.create_snapshot("pve1", 100, "vmigrate-abc", vmstate=False)   # waits for the task
    api.delete_snapshot("pve1", 100, "vmigrate-abc")

Token needs: VM.Audit, VM.Snapshot, Datastore.Audit (and Sys.Audit for node/storage listings).
"""

from __future__ import annotations

import time
import urllib.parse
from typing import Any

from ...core.errors import ProviderError, SafetyGateError, TransientProviderError

SNAP_PREFIX = "vmigrate-"


class PveApi:
    def __init__(self, url: str, token_id: str, token_secret: str, *, verify_tls: bool = True,
                 task_timeout: float = 3600, session: Any = None):
        import requests  # provider-private dependency
        self.base = url.rstrip("/") + "/api2/json"
        self.s = session or requests.Session()
        self.s.verify = verify_tls
        self.s.headers["Authorization"] = f"PVEAPIToken={token_id}={token_secret}"
        self.task_timeout = task_timeout

    def _call(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = self.s.request(method, self.base + path, timeout=120, **kw)
        except Exception as e:  # noqa: BLE001
            raise TransientProviderError(f"PVE {method} {path}: {e}") from e
        if r.status_code >= 500:
            raise TransientProviderError(f"PVE {method} {path}: HTTP {r.status_code} {r.text[:300]}")
        if r.status_code >= 400:
            raise ProviderError(f"PVE {method} {path}: HTTP {r.status_code} {r.text[:500]}")
        return r.json().get("data")

    def get(self, path: str, **params: Any) -> Any:
        return self._call("GET", path, params=params or None)

    # ------------------------------------------------------------------ inventory
    def version(self) -> dict[str, Any]:
        return self.get("/version")

    def vms(self) -> list[dict[str, Any]]:
        return [r for r in self.get("/cluster/resources", type="vm") or []
                if r.get("type") == "qemu" and not r.get("template")]

    def config(self, node: str, vmid: int) -> dict[str, Any]:
        return self.get(f"/nodes/{node}/qemu/{vmid}/config")

    def pending(self, node: str, vmid: int) -> list[dict[str, Any]]:
        return self.get(f"/nodes/{node}/qemu/{vmid}/pending") or []

    def storage(self, storage: str) -> dict[str, Any]:
        return self.get(f"/storage/{urllib.parse.quote(storage)}")

    def content(self, node: str, storage: str, vmid: int) -> list[dict[str, Any]]:
        return self.get(f"/nodes/{node}/storage/{urllib.parse.quote(storage)}/content", vmid=vmid) or []

    def node_storages(self, node: str) -> list[dict[str, Any]]:
        return self.get(f"/nodes/{node}/storage", enabled=1) or []

    def agent_osinfo(self, node: str, vmid: int) -> dict[str, Any] | None:
        try:
            return (self.get(f"/nodes/{node}/qemu/{vmid}/agent/get-osinfo") or {}).get("result")
        except ProviderError:
            return None

    def snapshots(self, node: str, vmid: int) -> list[dict[str, Any]]:
        return [s for s in self.get(f"/nodes/{node}/qemu/{vmid}/snapshot") or [] if s.get("name") != "current"]

    # ------------------------------------------------------------------ tasks + snapshots
    def wait_task(self, node: str, upid: str, what: str) -> None:
        deadline = time.monotonic() + self.task_timeout
        q = urllib.parse.quote(upid)
        while True:
            st = self.get(f"/nodes/{node}/tasks/{q}/status")
            if st.get("status") == "stopped":
                if st.get("exitstatus") != "OK":
                    raise ProviderError(f"{what} failed: {st.get('exitstatus')}")
                return
            if time.monotonic() > deadline:
                raise TransientProviderError(f"{what}: task {upid} still running after {self.task_timeout}s")
            time.sleep(2)

    def create_snapshot(self, node: str, vmid: int, name: str, *, vmstate: bool = False) -> None:
        if not name.startswith(SNAP_PREFIX):
            raise SafetyGateError("snapshot names must start with vmigrate-")
        upid = self._call("POST", f"/nodes/{node}/qemu/{vmid}/snapshot",
                          data={"snapname": name, "vmstate": int(vmstate),
                                "description": "vmigrate read point (safe to delete after the migration/sync)"})
        self.wait_task(node, upid, f"snapshot {name} of VM {vmid}")

    def delete_snapshot(self, node: str, vmid: int, name: str) -> None:
        if not name.startswith(SNAP_PREFIX):
            raise SafetyGateError(f"refusing to delete snapshot {name!r}: not created by vmigrate")
        if not any(s.get("name") == name for s in self.snapshots(node, vmid)):
            return
        upid = self._call("DELETE", f"/nodes/{node}/qemu/{vmid}/snapshot/{urllib.parse.quote(name)}")
        self.wait_task(node, upid, f"delete snapshot {name} of VM {vmid}")
