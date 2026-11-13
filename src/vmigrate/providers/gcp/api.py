"""Purpose: the slice of the Compute Engine v1 REST API the GCP source needs, without the Google
SDK: sign-in (service-account key or the GCE metadata server), instances, disks, machine types,
snapshots, temporary disks and attach/detach on the worker, with zone-operation waits.

Usage:
    g = GceApi(project="prod-123", key_json=None)        # None: metadata-server token (worker on GCE)
    g.instances(); g.instance(zone, name); g.disk(zone, name); g.machine_type(zone, "n2-standard-4")
    op = g.create_snapshot(zone, disk, snap_name, labels, guest_flush=False); g.wait(op)
    g.disk_from_snapshot(wzone, name, snapshot, labels, disk_type); g.attach(wzone, worker, disk, device)
    g.detach(wzone, worker, device); g.delete_disk(wzone, name); g.delete_snapshot(name)
    worker_identity()                                    # (project, zone, instance) from the metadata server

IAM: roles/compute.instanceAdmin.v1 on the worker (attach/detach, temporary disks) plus, on the
source project, compute.instances.get/list, compute.disks.get/list/createSnapshot,
compute.snapshots.create/get/delete/useReadOnly, compute.machineTypes.get
(roles/compute.storageAdmin + roles/compute.viewer cover them), compute.instances.stop for cutover.
NOT run against a live project yet: tests use a fake API.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.parse
from typing import Any

from ...core import cancel
from ...core.errors import ConfigError, ProviderError, SafetyGateError, TransientProviderError
from ..common.http import Http, default_http

COMPUTE = "https://compute.googleapis.com/compute/v1"
METADATA = "http://metadata.google.internal/computeMetadata/v1"
SCOPE = "https://www.googleapis.com/auth/compute"
LABEL = "vmigrate-run"
RETRY = (429, 500, 502, 503, 504)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _err(status: int, body: bytes) -> str:
    try:
        e = json.loads(body or b"{}").get("error")
    except ValueError:
        return f"HTTP {status}: {body[:300].decode(errors='replace')}"
    if isinstance(e, dict):
        return f"HTTP {status} {e.get('status') or e.get('code')}: {e.get('message')}"
    return f"HTTP {status}: {e}"


def last(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


class GceApi:
    def __init__(self, *, project: str, key_json: str | None = None, http: Http | None = None,
                 poll: float = 3, timeout: float = 6 * 3600, sleep: Any = None, now: Any = time.time):
        self.project = project
        self.key = json.loads(key_json) if key_json and key_json.strip() else None
        if self.key is not None and self.key.get("type") != "service_account":
            raise ConfigError("gcp: the key must be a service-account JSON key (type service_account)")
        self.http = http or default_http()
        self.poll, self.timeout = poll, timeout
        self.sleep = sleep or cancel.sleep
        self.now = now
        self._token: tuple[str, float] | None = None

    # ------------------------------------------------------------------ auth
    def _jwt(self) -> str:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        k = self.key or {}
        iat = int(self.now())
        head = _b64(json.dumps({"alg": "RS256", "typ": "JWT", "kid": k.get("private_key_id")}).encode())
        claims = _b64(json.dumps({"iss": k["client_email"], "scope": SCOPE, "iat": iat, "exp": iat + 3600,
                                  "aud": k.get("token_uri") or "https://oauth2.googleapis.com/token"}).encode())
        pk = serialization.load_pem_private_key(k["private_key"].encode(), password=None)
        sig = pk.sign(f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr,call-arg]
        return f"{head}.{claims}.{_b64(sig)}"

    def token(self) -> str:
        if self._token and self._token[1] > self.now() + 120:
            return self._token[0]
        if self.key is not None:
            uri = self.key.get("token_uri") or "https://oauth2.googleapis.com/token"
            body = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                           "assertion": self._jwt()}).encode()
            st, _, raw = self.http("POST", uri, {"Content-Type": "application/x-www-form-urlencoded"}, body, 30)
        else:
            st, _, raw = self.http("GET", f"{METADATA}/instance/service-accounts/default/token",
                                   {"Metadata-Flavor": "Google"}, None, 10)
        if st != 200:
            raise PermissionError(f"Google sign-in failed ({_err(st, raw)}). Check the service-account key, or "
                                  "that the worker VM has a service account with the compute scope.")
        d = json.loads(raw)
        self._token = (d["access_token"], self.now() + float(d.get("expires_in", 3600)))
        return self._token[0]

    def request(self, method: str, path: str, body: Any = None, *, ok: tuple[int, ...] = (200,),
                retries: int = 6) -> tuple[int, Any]:
        url = path if path.startswith("http") else f"{COMPUTE}/projects/{self.project}{path}"
        data = None if body is None else json.dumps(body).encode()
        for attempt in range(retries):
            h = {"Authorization": f"Bearer {self.token()}", "Accept": "application/json"}
            if data is not None:
                h["Content-Type"] = "application/json"
            try:
                st, _, raw = self.http(method, url, h, data, 120)
            except OSError as e:
                if attempt == retries - 1:
                    raise TransientProviderError(f"GCE {method} {path}: {e}") from e
                self.sleep(min(30.0, 2.0 * 2 ** attempt))
                continue
            if st in ok:
                return st, (json.loads(raw) if raw and raw.strip() else {})
            if st in RETRY and attempt < retries - 1:
                self.sleep(min(30.0, 2.0 * 2 ** attempt))
                continue
            msg = f"GCE {method} {url.split('?')[0].removeprefix(COMPUTE)}: {_err(st, raw)}"
            if st == 403:
                raise PermissionError(msg + " (the service account lacks this permission)")
            raise (TransientProviderError if st in RETRY else ProviderError)(msg)
        raise AssertionError("unreachable")

    def get(self, path: str) -> Any:
        return self.request("GET", path)[1]

    def get_or_none(self, path: str) -> Any:
        st, d = self.request("GET", path, ok=(200, 404))
        return None if st == 404 else d

    def wait(self, op: dict[str, Any]) -> dict[str, Any]:
        """Until a zone/global operation is DONE; raises its error, if any."""
        deadline = time.monotonic() + self.timeout
        while True:
            if op.get("status") == "DONE":
                if op.get("error"):
                    errs = (op["error"].get("errors") or [{}])
                    raise ProviderError(f"GCE {op.get('operationType')} {last(op.get('targetLink', ''))} failed: "
                                        + "; ".join(f"{e.get('code')}: {e.get('message')}" for e in errs))
                return op
            if time.monotonic() > deadline:
                raise TransientProviderError(f"GCE operation {op.get('name')} still running after {self.timeout:.0f}s")
            link = op.get("selfLink") or ""
            op = self.request("POST", f"{link}/wait")[1] if link else self.get(op["name"])
            if op.get("status") != "DONE":
                self.sleep(self.poll)

    # ------------------------------------------------------------------ inventory
    def instances(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        tok = None
        while True:
            q = "?maxResults=500" + (f"&pageToken={urllib.parse.quote(tok)}" if tok else "")
            d = self.get(f"/aggregated/instances{q}")
            for scoped in (d.get("items") or {}).values():
                out += scoped.get("instances") or []
            tok = d.get("nextPageToken")
            if not tok:
                return out

    def instance(self, zone: str, name: str) -> dict[str, Any]:
        return self.get(f"/zones/{zone}/instances/{name}")

    def disk(self, zone: str, name: str) -> dict[str, Any]:
        return self.get(f"/zones/{zone}/disks/{name}")

    def disks_used_by(self, zone: str, instance_link: str) -> list[dict[str, Any]]:
        out, tok = [], None
        while True:
            d = self.get(f"/zones/{zone}/disks?maxResults=500" + (f"&pageToken={urllib.parse.quote(tok)}" if tok else ""))
            out += [x for x in d.get("items") or [] if instance_link in (x.get("users") or [])]
            tok = d.get("nextPageToken")
            if not tok:
                return out

    def machine_type(self, zone: str, name: str) -> dict[str, Any]:
        return self.get(f"/zones/{zone}/machineTypes/{name}")

    def stop(self, zone: str, name: str) -> None:
        self.wait(self.request("POST", f"/zones/{zone}/instances/{name}/stop")[1])

    # ------------------------------------------------------------------ snapshots + temporary disks
    def create_snapshot(self, zone: str, disk: str, name: str, labels: dict[str, str], *, guest_flush: bool) -> dict[str, Any]:
        if not name.startswith("vmigrate-"):
            raise SafetyGateError("snapshot names must start with vmigrate-")
        q = "?guestFlush=true" if guest_flush else ""
        return self.request("POST", f"/zones/{zone}/disks/{disk}/createSnapshot{q}",
                            {"name": name, "labels": labels,
                             "description": "vmigrate read point; safe to delete after the migration/sync"})[1]

    def snapshot(self, name: str) -> dict[str, Any] | None:
        return self.get_or_none(f"/global/snapshots/{name}")

    def _ours(self, item: dict[str, Any] | None, what: str) -> bool:
        if item is None:
            return False
        if not str((item.get("labels") or {}).get(LABEL, "")).startswith("vmigrate-"):
            raise SafetyGateError(f"refusing to delete {what}: not labelled {LABEL}")
        return True

    def delete_snapshot(self, name: str) -> None:
        if self._ours(self.snapshot(name), f"snapshot {name}"):
            st, op = self.request("DELETE", f"/global/snapshots/{name}", ok=(200, 404))
            if st == 200:
                self.wait(op)

    def disk_from_snapshot(self, zone: str, name: str, snapshot: str, labels: dict[str, str], disk_type: str) -> None:
        if not name.startswith("vmigrate-"):
            raise SafetyGateError("temporary disk names must start with vmigrate-")
        self.wait(self.request("POST", f"/zones/{zone}/disks", {
            "name": name, "sourceSnapshot": f"projects/{self.project}/global/snapshots/{snapshot}",
            "type": f"projects/{self.project}/zones/{zone}/diskTypes/{disk_type}", "labels": labels})[1])

    def attach(self, zone: str, instance: str, disk: str, device: str) -> None:
        self.wait(self.request("POST", f"/zones/{zone}/instances/{instance}/attachDisk", {
            "source": f"projects/{self.project}/zones/{zone}/disks/{disk}", "deviceName": device,
            "mode": "READ_ONLY", "autoDelete": False})[1])

    def detach(self, zone: str, instance: str, device: str) -> None:
        st, op = self.request("POST", f"/zones/{zone}/instances/{instance}/detachDisk?deviceName={device}",
                              ok=(200, 400, 404))
        if st == 200:
            self.wait(op)

    def delete_disk(self, zone: str, name: str) -> None:
        if self._ours(self.get_or_none(f"/zones/{zone}/disks/{name}"), f"disk {name}"):
            st, op = self.request("DELETE", f"/zones/{zone}/disks/{name}", ok=(200, 404))
            if st == 200:
                self.wait(op)


def worker_identity(http: Http | None = None) -> tuple[str, str, str]:
    """(project, zone, instance name) of the GCE VM this worker runs on."""
    h = http or default_http()

    def md(path: str) -> str:
        st, _, raw = h("GET", f"{METADATA}/{path}", {"Metadata-Flavor": "Google"}, None, 5)
        if st != 200:
            raise ProviderError(f"GCE metadata {path}: HTTP {st}")
        return raw.decode().strip()

    try:
        return md("project/project-id"), last(md("instance/zone")), md("instance/name")
    except OSError as e:
        raise ProviderError("this worker is not a Compute Engine VM (no metadata server); the GCP source reads "
                            "disks by attaching them to a worker VM in Google Cloud") from e
