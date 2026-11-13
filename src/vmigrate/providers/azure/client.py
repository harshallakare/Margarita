"""Purpose: the slice of Azure Resource Manager (ARM) the Azure source needs, as plain REST calls:
sign-in, VMs, managed disks, NICs, VM sizes, incremental snapshots and their SAS access.

Sign-in (config `auth`):
  * client_secret (default): an Entra ID app registration (service principal); OAuth2 client
    credentials against login.microsoftonline.com/{tenant}/oauth2/v2.0/token
  * managed_identity: the worker is an Azure VM with a managed identity; token from the instance
    metadata service (169.254.169.254)
Clouds: public (default), usgov, china.

Usage:
    c = AzureClient(subscription_id=..., tenant_id=..., client_id=..., client_secret=...)
    c.vms(); c.vm(vm_id); c.disk(disk_id); c.nic(nic_id); c.vm_size(location, "Standard_D4s_v5")
    sid = c.start_snapshot(rg, name, location, disk_id, run_tag)  # incremental; then c.wait_snapshot(sid)
    sas = c.grant_access(snapshot_id, seconds=8 * 3600)          # read-only SAS URL (keep in memory only)
    c.revoke_access(snapshot_id); c.delete_snapshot(snapshot_id) # only snapshots tagged vmigrate:run

Azure RBAC the identity needs (custom role, or Reader + Disk Snapshot Contributor on the scope):
    Microsoft.Compute/virtualMachines/read, Microsoft.Compute/disks/read,
    Microsoft.Compute/locations/vmSizes/read, Microsoft.Network/networkInterfaces/read,
    Microsoft.Compute/snapshots/read|write|delete,
    Microsoft.Compute/snapshots/beginGetAccess/action, Microsoft.Compute/snapshots/endGetAccess/action,
    Microsoft.Compute/virtualMachines/powerOff/action only for the opt-in cutover shutdown.
The source disks themselves are never exported, written or detached.

API shapes follow Microsoft's published REST reference. NOT run against a live subscription yet
(the tests use a fake ARM).
"""

from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any

from ...core import cancel
from ...core.errors import ConfigError, ProviderError, SafetyGateError, TransientProviderError
from ..common.http import Http, default_http

TAG = "vmigrate:run"
COMPUTE_API = "2024-07-01"
DISK_API = "2024-03-02"
NETWORK_API = "2024-05-01"

CLOUDS = {
    "public": ("https://login.microsoftonline.com", "https://management.azure.com"),
    "usgov": ("https://login.microsoftonline.us", "https://management.usgovcloudapi.net"),
    "china": ("https://login.chinacloudapi.cn", "https://management.chinacloudapi.cn"),
}
IMDS = "http://169.254.169.254/metadata/identity/oauth2/token"
RETRY = (408, 429, 500, 502, 503, 504)


def _err(status: int, body: bytes) -> str:
    try:
        d = json.loads(body or b"{}")
    except ValueError:
        return f"HTTP {status}: {body[:300].decode(errors='replace')}"
    e = d.get("error")
    if isinstance(e, dict):
        return f"HTTP {status} {e.get('code')}: {e.get('message')}"
    if d.get("error_description"):
        return f"HTTP {status} {d.get('error')}: {str(d['error_description']).splitlines()[0]}"
    return f"HTTP {status}: {str(d)[:300]}"


def split_id(rid: str) -> dict[str, str]:
    """/subscriptions/S/resourceGroups/RG/providers/P/T/NAME -> {"subscriptions": S, "resourcegroups": RG, ...}"""
    parts = [p for p in rid.split("/") if p]
    return {parts[i].lower(): parts[i + 1] for i in range(0, len(parts) - 1, 2)}


class AzureClient:
    def __init__(self, *, subscription_id: str, tenant_id: str | None = None, client_id: str | None = None,
                 client_secret: str | None = None, auth: str = "client_secret", cloud: str = "public",
                 managed_identity_client_id: str | None = None, http: Http | None = None,
                 poll: float = 5, timeout: float = 6 * 3600, sleep: Any = None):
        if cloud not in CLOUDS:
            raise ConfigError(f"azure: unknown cloud {cloud!r}; one of {', '.join(CLOUDS)}")
        if auth not in ("client_secret", "managed_identity"):
            raise ConfigError("azure: auth must be client_secret or managed_identity")
        if auth == "client_secret" and not (tenant_id and client_id and client_secret):
            raise ConfigError("azure: tenant_id, client_id and client_secret are required (or auth: managed_identity)")
        self.sub, self.tenant, self.client_id, self.secret = subscription_id, tenant_id, client_id, client_secret
        self.auth, self.mi_client_id = auth, managed_identity_client_id
        self.login, self.arm = CLOUDS[cloud]
        self.http = http or default_http()
        self.poll, self.timeout = poll, timeout
        self.sleep = sleep or cancel.sleep
        self._token: tuple[str, float] | None = None

    # ------------------------------------------------------------------ auth + transport
    def _get_token(self) -> str:
        if self._token and self._token[1] > time.time() + 120:
            return self._token[0]
        if self.auth == "managed_identity":
            q = {"api-version": "2018-02-01", "resource": self.arm + "/"}
            if self.mi_client_id:
                q["client_id"] = self.mi_client_id
            st, _, raw = self.http("GET", f"{IMDS}?{urllib.parse.urlencode(q)}", {"Metadata": "true"}, None, 20)
        else:
            body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self.client_id,
                                           "client_secret": self.secret, "scope": self.arm + "/.default"}).encode()
            tenant = urllib.parse.quote(str(self.tenant).strip(), safe="")
            st, _, raw = self.http("POST", f"{self.login}/{tenant}/oauth2/v2.0/token",
                                   {"Content-Type": "application/x-www-form-urlencoded"}, body, 30)
        if st != 200:
            raise PermissionError(f"Azure sign-in failed ({_err(st, raw)}). Check the tenant ID, client ID and "
                                  "client secret (secrets expire), or the VM's managed identity.")
        d = json.loads(raw)
        self._token = (d["access_token"], time.time() + float(d.get("expires_in", 3600)))
        return self._token[0]

    def request(self, method: str, path_or_url: str, *, api: str | None = None, body: Any = None,
                ok: tuple[int, ...] = (200,), retries: int = 6) -> tuple[int, dict[str, str], Any]:
        url = path_or_url if path_or_url.startswith("http") else self.arm + path_or_url
        if api and "api-version=" not in url:
            url += ("&" if "?" in url else "?") + "api-version=" + api
        data = None if body is None else json.dumps(body).encode()
        for attempt in range(retries):
            h = {"Authorization": f"Bearer {self._get_token()}", "Accept": "application/json"}
            if data is not None:
                h["Content-Type"] = "application/json"
            try:
                st, hdr, raw = self.http(method, url, h, data, 60)
            except OSError as e:
                if attempt == retries - 1:
                    raise TransientProviderError(f"Azure {method} {url.split('?')[0]}: {e}") from e
                self.sleep(min(30.0, 2.0 * 2 ** attempt))
                continue
            if st in ok:
                return st, hdr, (json.loads(raw) if raw and raw.strip() else {})
            if st in RETRY and attempt < retries - 1:
                wait = float(hdr.get("retry-after") or 0) or min(30.0, 2.0 * 2 ** attempt)
                self.sleep(min(wait, 120.0))
                continue
            if st == 401:
                self._token = None
            msg = f"Azure {method} {url.split('?')[0].removeprefix(self.arm)}: {_err(st, raw)}"
            if st in RETRY:
                raise TransientProviderError(msg)
            if st == 403:
                raise PermissionError(msg + " (the identity's Azure role lacks this permission)")
            raise ProviderError(msg)
        raise AssertionError("unreachable")

    def get(self, path: str, api: str) -> dict[str, Any]:
        return self.request("GET", path, api=api)[2]

    def get_or_none(self, path: str, api: str) -> dict[str, Any] | None:
        st, _, d = self.request("GET", path, api=api, ok=(200, 404))
        return None if st == 404 else d

    def list(self, path: str, api: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        url: str | None = path
        while url:
            d = self.request("GET", url, api=api if not url.startswith("http") else None)[2]
            out += d.get("value") or []
            url = d.get("nextLink")
        return out

    def wait_async(self, headers: dict[str, str], what: str) -> dict[str, Any]:
        """Follow an ARM long-running operation (Azure-AsyncOperation or Location header)."""
        deadline = time.monotonic() + self.timeout
        op, loc = headers.get("azure-asyncoperation"), headers.get("location")
        while True:
            if time.monotonic() > deadline:
                raise TransientProviderError(f"Azure: {what} still running after {self.timeout:.0f}s")
            if op:
                st, _, d = self.request("GET", op, ok=(200, 202))
                s = str(d.get("status") or "").lower() if st == 200 else "inprogress"
                if s == "succeeded":
                    if loc:
                        st, _, out = self.request("GET", loc, ok=(200, 202, 204))
                        if st == 200:
                            return out
                    return d.get("properties", {}).get("output") or d
                if s in ("failed", "canceled", "cancelled"):
                    raise ProviderError(f"Azure: {what} {s}: {(d.get('error') or {}).get('message') or d}")
            elif loc:
                st, _, d = self.request("GET", loc, ok=(200, 202, 204))
                if st in (200, 204):
                    return d
            else:
                return {}
            self.sleep(self.poll)

    # ------------------------------------------------------------------ inventory
    def subscription(self) -> dict[str, Any]:
        return self.get(f"/subscriptions/{self.sub}", "2022-12-01")

    def vms(self, resource_group: str | None = None) -> list[dict[str, Any]]:
        scope = f"/subscriptions/{self.sub}" + (f"/resourceGroups/{resource_group}" if resource_group else "")
        return self.list(f"{scope}/providers/Microsoft.Compute/virtualMachines", COMPUTE_API)

    def vm(self, vm_id: str) -> dict[str, Any]:
        return self.get(f"{vm_id}?$expand=instanceView", COMPUTE_API)

    def disk(self, disk_id: str) -> dict[str, Any]:
        return self.get(disk_id, DISK_API)

    def disks_managed_by(self, vm_id: str) -> list[dict[str, Any]]:
        """Second, independent disk enumeration for the cross-check: every managed disk in the
        subscription whose managedBy is this VM (also catches disks in other resource groups)."""
        return [d for d in self.list(f"/subscriptions/{self.sub}/providers/Microsoft.Compute/disks", DISK_API)
                if str(d.get("managedBy") or "").lower() == vm_id.lower()]

    def nic(self, nic_id: str) -> dict[str, Any]:
        return self.get(nic_id, NETWORK_API)

    def vm_size(self, location: str, size: str) -> dict[str, Any]:
        for s in self.list(f"/subscriptions/{self.sub}/providers/Microsoft.Compute/locations/{location}/vmSizes",
                           COMPUTE_API):
            if str(s.get("name")).lower() == size.lower():
                return s
        raise ProviderError(f"azure: VM size {size} not listed in {location}")

    def power_off(self, vm_id: str, *, force: bool) -> str:
        v = self.vm(vm_id)
        if power_state(v) in ("stopped", "deallocated"):
            return f"already {power_state(v)}"
        q = "?skipShutdown=true" if force else ""
        _, h, _ = self.request("POST", f"{vm_id}/powerOff{q}", api=COMPUTE_API, ok=(200, 202))
        self.wait_async(h, "power off")
        return "stopped"

    # ------------------------------------------------------------------ snapshots
    def snapshot_path(self, rg: str, name: str) -> str:
        return f"/subscriptions/{self.sub}/resourceGroups/{rg}/providers/Microsoft.Compute/snapshots/{name}"

    def start_snapshot(self, rg: str, name: str, location: str, disk_id: str, run_tag: str) -> str:
        """Start an incremental snapshot of one managed disk (it stores only changed pages, and the
        page-range diff needs it). Returns the snapshot's resource id; wait_snapshot() waits for it."""
        if not run_tag.startswith("vmigrate-") or not name.startswith("vmigrate-"):
            raise SafetyGateError("snapshot names and run tags must start with vmigrate-")
        path = self.snapshot_path(rg, name)
        body = {"location": location,
                "tags": {TAG: run_tag, "vmigrate:note": "read point of a migration/sync; safe to delete after it"},
                "properties": {"incremental": True,
                               "creationData": {"createOption": "Copy", "sourceResourceId": disk_id}}}
        self.request("PUT", path, api=DISK_API, body=body, ok=(200, 201, 202))
        return path

    def wait_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        """Until provisioningState is Succeeded and the snapshot is fully copied (completionPercent;
        Ultra / Premium SSD v2 disks copy in the background and cannot be read before 100%)."""
        deadline = time.monotonic() + self.timeout
        while True:
            s = self.get(snapshot_id, DISK_API)
            p = s.get("properties") or {}
            state = str(p.get("provisioningState") or "")
            if state == "Failed":
                raise ProviderError(f"azure: snapshot {snapshot_id.rsplit('/', 1)[-1]} failed")
            pct = p.get("completionPercent")
            if state == "Succeeded" and (pct is None or float(pct) >= 100):
                return s
            if time.monotonic() > deadline:
                raise TransientProviderError(f"azure: snapshot {snapshot_id.rsplit('/', 1)[-1]} not ready after "
                                             f"{self.timeout:.0f}s (state {state}, {pct}% copied)")
            self.sleep(self.poll)

    def _ours(self, snapshot_id: str) -> dict[str, Any] | None:
        s = self.get_or_none(snapshot_id, DISK_API)
        if s is not None and not str((s.get("tags") or {}).get(TAG, "")).startswith("vmigrate-"):
            raise SafetyGateError(f"refusing to touch {snapshot_id}: not tagged {TAG}")
        return s

    def grant_access(self, snapshot_id: str, seconds: int) -> str:
        if self._ours(snapshot_id) is None:
            raise ProviderError(f"azure: snapshot {snapshot_id} not found")
        _, h, d = self.request("POST", f"{snapshot_id}/beginGetAccess", api=DISK_API,
                               body={"access": "Read", "durationInSeconds": int(seconds)}, ok=(200, 202))
        if not d.get("accessSAS"):
            d = self.wait_async(h, "grant snapshot access")
        sas = d.get("accessSAS") or ((d.get("properties") or {}).get("output") or {}).get("accessSAS")
        if not sas:
            raise ProviderError(f"azure: beginGetAccess on {snapshot_id} returned no SAS")
        return str(sas)

    def revoke_access(self, snapshot_id: str) -> None:
        if self._ours(snapshot_id) is None:
            return
        _, h, _ = self.request("POST", f"{snapshot_id}/endGetAccess", api=DISK_API, ok=(200, 202))
        self.wait_async(h, "end snapshot access")

    def delete_snapshot(self, snapshot_id: str) -> None:
        if self._ours(snapshot_id) is None:
            return
        self.revoke_access(snapshot_id)      # a snapshot with an active SAS cannot be deleted
        st, h, _ = self.request("DELETE", snapshot_id, api=DISK_API, ok=(200, 202, 204, 404))
        if st == 202:
            self.wait_async(h, "delete snapshot")


def power_state(vm: dict[str, Any]) -> str:
    for s in ((vm.get("properties") or {}).get("instanceView") or {}).get("statuses") or []:
        code = str(s.get("code") or "")
        if code.startswith("PowerState/"):
            return code.split("/", 1)[1]
    return "unknown"
