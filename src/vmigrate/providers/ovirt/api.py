"""Purpose: the slice of the oVirt / RHV / OLVM REST API v4 the source needs (JSON, no SDK):
VMs, disk attachments, disks, NICs, backups, checkpoints and image transfers.

Usage:
    a = OvirtApi("https://engine.example.com", "admin@internal", password, ca_pem=..., http=None)
    a.vms(); a.vm(id); a.disk_attachments(id); a.disk(disk_id); a.nics(id); a.network_of_profile(pid)
    b = a.start_backup(vm_id, [disk_ids], from_checkpoint, description, require_consistency)
    a.wait_backup(vm_id, b["id"])               # phase ready
    t = a.start_transfer(disk_id, backup_id)    # {id, transfer_url, proxy_url}, phase transferring
    a.finalize_transfer(t["id"]); a.finalize_backup(vm_id, b["id"])
    a.checkpoints(vm_id); a.delete_checkpoint(vm_id, cid)

Permissions: a user with the DiskBackup / Backup role (or admin) on the VMs; image transfers need
the hosts' ImageIO (TCP 54322) reachable from the worker, or the engine's proxy (54323).
API shapes follow the oVirt 4.4/4.5 REST reference; NOT run against a live engine yet.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.parse
from typing import Any

from ...core import cancel
from ...core.errors import ProviderError, TransientProviderError
from ..common.http import Http, default_http, tls_context

RETRY = (429, 502, 503, 504)


def _err(status: int, body: bytes) -> str:
    try:
        d = json.loads(body or b"{}")
    except ValueError:
        return f"HTTP {status}: {body[:300].decode(errors='replace')}"
    return f"HTTP {status}: {d.get('reason') or ''} {d.get('detail') or d.get('fault', {}).get('detail') or ''}".strip()


class OvirtApi:
    def __init__(self, url: str, username: str, password: str, *, ca_pem: str | None = None, verify_tls: bool = True,
                 http: Http | None = None, poll: float = 2, timeout: float = 3600, sleep: Any = None):
        base = url.rstrip("/")
        if not base.endswith("/ovirt-engine/api"):
            base += "/ovirt-engine/api"
        self.base = base
        self.ctx = tls_context(verify_tls, ca_pem)
        self.http = http or default_http(self.ctx)
        self.auth = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        self.poll, self.timeout = poll, timeout
        self.sleep = sleep or cancel.sleep

    def request(self, method: str, path: str, body: Any = None, *, ok: tuple[int, ...] = (200, 201, 202),
                retries: int = 5) -> tuple[int, Any]:
        url = path if path.startswith("http") else self.base + path
        data = None if body is None else json.dumps(body).encode()
        h = {"Authorization": self.auth, "Accept": "application/json", "Version": "4"}
        if data is not None:
            h["Content-Type"] = "application/json"
        for attempt in range(retries):
            try:
                st, _, raw = self.http(method, url, h, data, 120)
            except OSError as e:
                if attempt == retries - 1:
                    raise TransientProviderError(f"oVirt {method} {path}: {e}") from e
                self.sleep(2.0 * 2 ** attempt)
                continue
            if st in ok:
                return st, (json.loads(raw) if raw and raw.strip() else {})
            if st in RETRY and attempt < retries - 1:
                self.sleep(2.0 * 2 ** attempt)
                continue
            msg = f"oVirt {method} {path.split('?')[0]}: {_err(st, raw)}"
            if st == 401:
                raise PermissionError(msg + " (check the user name, e.g. admin@internal, and password)")
            raise (TransientProviderError if st in RETRY else ProviderError)(msg)
        raise AssertionError("unreachable")

    def get(self, path: str) -> Any:
        return self.request("GET", path, ok=(200,))[1]

    def get_or_none(self, path: str) -> Any:
        st, d = self.request("GET", path, ok=(200, 404))
        return None if st == 404 else d

    # ------------------------------------------------------------------ inventory
    def version(self) -> str:
        v = (self.get("").get("product_info") or {}).get("version") or {}
        return str(v.get("full_version") or f"{v.get('major')}.{v.get('minor')}")

    def vms(self) -> list[dict[str, Any]]:
        return self.get("/vms").get("vm") or []

    def vm(self, vm_id: str) -> dict[str, Any]:
        return self.get(f"/vms/{vm_id}")

    def disk_attachments(self, vm_id: str) -> list[dict[str, Any]]:
        return self.get(f"/vms/{vm_id}/diskattachments").get("disk_attachment") or []

    def disk(self, disk_id: str) -> dict[str, Any]:
        return self.get(f"/disks/{disk_id}")

    def disks_by_vm_name(self, name: str) -> list[dict[str, Any]]:
        q = urllib.parse.quote(f"vm_names={name}", safe="")
        return self.get(f"/disks?search={q}").get("disk") or []

    def nics(self, vm_id: str) -> list[dict[str, Any]]:
        return self.get(f"/vms/{vm_id}/nics").get("nic") or []

    def network_of_profile(self, profile_id: str) -> str:
        p = self.get_or_none(f"/vnicprofiles/{profile_id}") or {}
        nid = (p.get("network") or {}).get("id")
        n = self.get_or_none(f"/networks/{nid}") if nid else None
        return str((n or {}).get("name") or p.get("name") or profile_id)

    def shutdown(self, vm_id: str, *, force: bool) -> None:
        self.request("POST", f"/vms/{vm_id}/{'stop' if force else 'shutdown'}", {})

    # ------------------------------------------------------------------ backups / checkpoints
    def start_backup(self, vm_id: str, disk_ids: list[str], from_checkpoint: str | None, description: str,
                     require_consistency: bool) -> dict[str, Any]:
        body: dict[str, Any] = {"disks": {"disk": [{"id": d} for d in disk_ids]}, "description": description}
        if from_checkpoint:
            body["from_checkpoint_id"] = from_checkpoint
        q = "?require_consistency=true" if require_consistency else ""
        return self.request("POST", f"/vms/{vm_id}/backups{q}", body)[1]

    def backup(self, vm_id: str, backup_id: str) -> dict[str, Any] | None:
        return self.get_or_none(f"/vms/{vm_id}/backups/{backup_id}")

    def wait_backup(self, vm_id: str, backup_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        while True:
            b = self.backup(vm_id, backup_id)
            phase = str((b or {}).get("phase") or "gone")
            if phase == "ready":
                return b or {}
            if phase in ("failed", "gone", "finalizing", "succeeded"):
                raise ProviderError(f"oVirt backup {backup_id} is {phase} instead of ready")
            if time.monotonic() > deadline:
                raise TransientProviderError(f"oVirt backup {backup_id} not ready after {self.timeout:.0f}s ({phase})")
            self.sleep(self.poll)

    def finalize_backup(self, vm_id: str, backup_id: str) -> None:
        if self.backup(vm_id, backup_id) is None:
            return
        self.request("POST", f"/vms/{vm_id}/backups/{backup_id}/finalize", {}, ok=(200, 201, 202, 404))

    def checkpoints(self, vm_id: str) -> list[dict[str, Any]]:
        return self.get(f"/vms/{vm_id}/checkpoints").get("checkpoint") or []

    def delete_checkpoint(self, vm_id: str, cid: str) -> None:
        self.request("DELETE", f"/vms/{vm_id}/checkpoints/{cid}", ok=(200, 202, 204, 404))

    # ------------------------------------------------------------------ image transfers
    def start_transfer(self, disk_id: str, backup_id: str, *, inactivity: int = 3600) -> dict[str, Any]:
        t = self.request("POST", "/imagetransfers", {
            "disk": {"id": disk_id}, "backup": {"id": backup_id}, "direction": "download", "format": "raw",
            "inactivity_timeout": inactivity})[1]
        deadline = time.monotonic() + 600
        while True:
            phase = str(t.get("phase") or "")
            if phase == "transferring":
                return t
            if "fail" in phase or phase in ("cancelled_system", "cancelled_user", "finished_failure"):
                raise ProviderError(f"oVirt image transfer for disk {disk_id} failed ({phase})")
            if time.monotonic() > deadline:
                raise TransientProviderError(f"oVirt image transfer for disk {disk_id} stuck in {phase}")
            self.sleep(self.poll)
            t = self.get(f"/imagetransfers/{t['id']}")

    def finalize_transfer(self, transfer_id: str) -> None:
        self.request("POST", f"/imagetransfers/{transfer_id}/finalize", {}, ok=(200, 201, 202, 404))
