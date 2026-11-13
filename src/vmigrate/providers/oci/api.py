"""Purpose: the slice of the OCI Core Services API (20160918) the OCI source needs, without the OCI
SDK: request signing with an API key, instances, boot/block volume attachments, volumes, images,
VNICs, volume backups, restores, attach/detach to the worker, with lifecycle waits.

Request signing (OCI HTTP signature, rsa-sha256): signed headers are date, (request-target), host,
and for bodies x-content-sha256, content-type, content-length; keyId = tenancy/user/fingerprint.

Usage:
    o = OciApi(region="eu-frankfurt-1", tenancy=..., user=..., fingerprint=..., key_pem=...)
    o.instances(compartment); o.instance(id); o.boot_attachments(ad, comp, iid); o.volume_attachments(comp, iid)
    o.backup_volume(vol_id, boot, name, tags)  -> backup id;  o.wait_state(path, "AVAILABLE")
    o.restore(backup_id, boot, ad, comp, name, tags) -> volume id; o.attach(worker, vol, device) -> attachment id
    o.detach(attachment); o.delete_volume(id, boot); o.delete_backup(id, boot)
    worker_identity()        # {id, availabilityDomain, compartmentId} from the instance metadata service

IAM policy (example): allow group margarita to manage volume-family in compartment X; to read
instance-family / virtual-network-family in compartment X; to use instances in compartment of the worker.
NOT run against a live tenancy yet: tests use a fake API.
"""

from __future__ import annotations

import base64
import email.utils
import hashlib
import json
import time
import urllib.parse
from typing import Any

from ...core import cancel
from ...core.errors import ConfigError, ProviderError, SafetyGateError, TransientProviderError
from ..common.http import Http, default_http

TAG = "vmigrate-run"
IMDS = "http://169.254.169.254/opc/v2/instance/"
RETRY = (429, 500, 502, 503, 504)


def _err(status: int, body: bytes) -> str:
    try:
        d = json.loads(body or b"{}")
        return f"HTTP {status} {d.get('code')}: {d.get('message')}"
    except ValueError:
        return f"HTTP {status}: {body[:300].decode(errors='replace')}"


class OciApi:
    def __init__(self, *, region: str, tenancy: str, user: str, fingerprint: str, key_pem: str,
                 http: Http | None = None, poll: float = 5, timeout: float = 6 * 3600, sleep: Any = None,
                 now: Any = None):
        for k, v in (("region", region), ("tenancy", tenancy), ("user", user), ("fingerprint", fingerprint),
                     ("API key", key_pem)):
            if not v:
                raise ConfigError(f"oci: {k} is required")
        from cryptography.hazmat.primitives import serialization
        self.key = serialization.load_pem_private_key(key_pem.encode(), password=None)
        self.key_id = f"{tenancy}/{user}/{fingerprint}"
        self.tenancy = tenancy
        self.base = f"https://iaas.{region}.oraclecloud.com/20160918"
        self.http = http or default_http()
        self.poll, self.timeout = poll, timeout
        self.sleep = sleep or cancel.sleep
        self.now = now or time.time

    def _sign(self, method: str, url: str, body: bytes | None) -> dict[str, str]:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        u = urllib.parse.urlsplit(url)
        target = f"{method.lower()} {u.path}" + (f"?{u.query}" if u.query else "")
        h = {"date": email.utils.formatdate(self.now(), usegmt=True), "host": u.netloc}
        names = ["date", "(request-target)", "host"]
        if method in ("POST", "PUT", "PATCH"):
            b = body or b""
            h["x-content-sha256"] = base64.b64encode(hashlib.sha256(b).digest()).decode()
            h["content-type"] = "application/json"
            h["content-length"] = str(len(b))
            names += ["x-content-sha256", "content-type", "content-length"]
        signing = "\n".join(f"{n}: {target if n == '(request-target)' else h[n]}" for n in names)
        sig = base64.b64encode(self.key.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()  # type: ignore[union-attr,call-arg]
        h["authorization"] = (f'Signature version="1",keyId="{self.key_id}",algorithm="rsa-sha256",'
                              f'headers="{" ".join(names)}",signature="{sig}"')
        return h

    def request(self, method: str, path: str, body: Any = None, *, ok: tuple[int, ...] = (200,),
                retries: int = 6) -> tuple[int, dict[str, str], Any]:
        url = path if path.startswith("http") else self.base + path
        data = None if body is None else json.dumps(body).encode()
        for attempt in range(retries):
            h = self._sign(method, url, data)
            h["accept"] = "application/json"
            try:
                st, hdr, raw = self.http(method, url, h, data, 120)
            except OSError as e:
                if attempt == retries - 1:
                    raise TransientProviderError(f"OCI {method} {path}: {e}") from e
                self.sleep(min(30.0, 2.0 * 2 ** attempt))
                continue
            if st in ok:
                return st, hdr, (json.loads(raw) if raw and raw.strip() else {})
            if st in RETRY and attempt < retries - 1:
                self.sleep(min(30.0, 2.0 * 2 ** attempt))
                continue
            msg = f"OCI {method} {path.split('?')[0]}: {_err(st, raw)}"
            if st in (401, 403):
                raise PermissionError(msg + " (check the API key / fingerprint and the IAM policy)")
            raise (TransientProviderError if st in RETRY else ProviderError)(msg)
        raise AssertionError("unreachable")

    def get(self, path: str) -> Any:
        return self.request("GET", path)[2]

    def get_or_none(self, path: str) -> Any:
        st, _, d = self.request("GET", path, ok=(200, 404))
        return None if st == 404 else d

    def list(self, path: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = None
        while True:
            p = path + (("&" if "?" in path else "?") + f"page={urllib.parse.quote(page)}" if page else "")
            _, hdr, d = self.request("GET", p)
            out += d if isinstance(d, list) else d.get("items") or []
            page = hdr.get("opc-next-page")
            if not page:
                return out

    def wait_state(self, path: str, want: str, *, gone_ok: bool = False) -> dict[str, Any] | None:
        deadline = time.monotonic() + self.timeout
        while True:
            d = self.get_or_none(path)
            if d is None:
                if gone_ok:
                    return None
                raise ProviderError(f"OCI {path} disappeared")
            st = str(d.get("lifecycleState") or "")
            if st == want:
                return d
            if st in ("FAULTY", "FAILED", "TERMINATED") and want not in ("TERMINATED", "DETACHED"):
                raise ProviderError(f"OCI {path.rsplit('/', 1)[-1]} is {st}")
            if time.monotonic() > deadline:
                raise TransientProviderError(f"OCI {path.rsplit('/', 1)[-1]} still {st} after {self.timeout:.0f}s")
            self.sleep(self.poll)

    # ------------------------------------------------------------------ inventory
    def q(self, **kw: str) -> str:
        return urllib.parse.urlencode({k: v for k, v in kw.items() if v})

    def instances(self, compartment: str) -> list[dict[str, Any]]:
        return [i for i in self.list(f"/instances?{self.q(compartmentId=compartment)}")
                if i.get("lifecycleState") not in ("TERMINATED", "TERMINATING")]

    def instance(self, iid: str) -> dict[str, Any]:
        return self.get(f"/instances/{iid}")

    def boot_attachments(self, ad: str, compartment: str, iid: str) -> list[dict[str, Any]]:
        return [a for a in self.list(f"/bootVolumeAttachments?{self.q(availabilityDomain=ad, compartmentId=compartment, instanceId=iid)}")
                if a.get("lifecycleState") == "ATTACHED"]

    def volume_attachments(self, compartment: str, iid: str | None = None) -> list[dict[str, Any]]:
        return [a for a in self.list(f"/volumeAttachments?{self.q(compartmentId=compartment, instanceId=iid or '')}")
                if a.get("lifecycleState") in ("ATTACHED", "ATTACHING")]

    def volume(self, vid: str, boot: bool) -> dict[str, Any]:
        return self.get(f"/{'bootVolumes' if boot else 'volumes'}/{vid}")

    def image(self, image_id: str) -> dict[str, Any] | None:
        return self.get_or_none(f"/images/{image_id}") if image_id else None

    def vnics(self, compartment: str, iid: str) -> list[dict[str, Any]]:
        out = []
        for a in self.list(f"/vnicAttachments?{self.q(compartmentId=compartment, instanceId=iid)}"):
            if a.get("lifecycleState") == "ATTACHED" and a.get("vnicId"):
                out.append(self.get(f"/vnics/{a['vnicId']}"))
        return out

    def stop(self, iid: str, *, force: bool) -> None:
        self.request("POST", f"/instances/{iid}?action={'STOP' if force else 'SOFTSTOP'}")
        self.wait_state(f"/instances/{iid}", "STOPPED")

    # ------------------------------------------------------------------ backups / restores / attach
    def backup_volume(self, vid: str, boot: bool, name: str, tags: dict[str, str]) -> str:
        if not name.startswith("vmigrate-"):
            raise SafetyGateError("backup names must start with vmigrate-")
        body = {"displayName": name, "type": "FULL", "freeformTags": tags,
                ("bootVolumeId" if boot else "volumeId"): vid}
        return self.request("POST", "/bootVolumeBackups" if boot else "/volumeBackups", body)[2]["id"]

    def backup_path(self, bid: str, boot: bool) -> str:
        return f"/{'bootVolumeBackups' if boot else 'volumeBackups'}/{bid}"

    def restore(self, bid: str, boot: bool, ad: str, compartment: str, name: str, tags: dict[str, str]) -> str:
        body = {"availabilityDomain": ad, "compartmentId": compartment, "displayName": name, "freeformTags": tags,
                "sourceDetails": {"type": "bootVolumeBackup" if boot else "volumeBackup", "id": bid}}
        vid = self.request("POST", "/bootVolumes" if boot else "/volumes", body)[2]["id"]
        self.wait_state(f"/{'bootVolumes' if boot else 'volumes'}/{vid}", "AVAILABLE")
        return vid

    def free_device(self, worker: str) -> str:
        devs = self.list(f"/instances/{worker}/devices?isAvailable=true")
        if not devs:
            raise ProviderError("OCI: no free device path on the worker (consistent device paths required)")
        return str(devs[0]["name"])

    def attach(self, worker: str, vid: str, device: str, name: str) -> str:
        aid = self.request("POST", "/volumeAttachments", {
            "type": "paravirtualized", "instanceId": worker, "volumeId": vid, "isReadOnly": True,
            "device": device, "displayName": name})[2]["id"]
        self.wait_state(f"/volumeAttachments/{aid}", "ATTACHED")
        return aid

    def detach(self, aid: str) -> None:
        st, _, _ = self.request("DELETE", f"/volumeAttachments/{aid}", ok=(200, 204, 404))
        if st != 404:
            self.wait_state(f"/volumeAttachments/{aid}", "DETACHED", gone_ok=True)

    def _ours(self, d: dict[str, Any] | None, what: str) -> bool:
        if d is None or d.get("lifecycleState") in ("TERMINATED", "DELETED"):
            return False
        if not str((d.get("freeformTags") or {}).get(TAG, "")).startswith("vmigrate-"):
            raise SafetyGateError(f"refusing to delete {what}: not tagged {TAG}")
        return True

    def delete_volume(self, vid: str, boot: bool) -> None:
        path = f"/{'bootVolumes' if boot else 'volumes'}/{vid}"
        if self._ours(self.get_or_none(path), path):
            self.request("DELETE", path, ok=(200, 204, 404))

    def delete_backup(self, bid: str, boot: bool) -> None:
        path = self.backup_path(bid, boot)
        if self._ours(self.get_or_none(path), path):
            self.request("DELETE", path, ok=(200, 204, 404))


def worker_identity(http: Http | None = None) -> dict[str, Any]:
    h = http or default_http()
    try:
        st, _, raw = h("GET", IMDS, {"Authorization": "Bearer Oracle"}, None, 5)
    except OSError as e:
        raise ProviderError("this worker is not an OCI instance (no metadata service); the OCI source reads disks by "
                            "attaching them to a worker instance in the same region") from e
    if st != 200:
        raise ProviderError(f"OCI instance metadata: HTTP {st}")
    return json.loads(raw)
