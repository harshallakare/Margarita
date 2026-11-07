"""Purpose: the Prism Central **v4** API calls behind the Nutanix iSCSI transport (no NFS):
recovery point -> temporary Volume Group whose disks use the recovery point as data source
(instant, no copy) -> attach this worker's iSCSI initiator -> (iscsi.py reads) -> detach, delete.
Same flow Coriolis documents for its Nutanix plugin (PC >= 7.3, AOS >= 6.8).

Usage:
    v4 = PrismV4(session, "https://pc:9440")              # session: authenticated requests.Session
    v4.clusters(); v4.data_services_ip(cluster_ext)
    rp = v4.create_recovery_point(vm_ext, "vmigrate-abc", app_consistent=True)
    disks = v4.recovery_point_disks(rp)                   # {vm_disk_ext: disk_recovery_point_ext}
    vg = v4.create_volume_group("vmigrate-abc", cluster_ext)
    v4.add_disk_from_recovery_point(vg, rp, disk_rp_ext, index)
    v4.attach_iscsi_client(vg, "iqn.1994-05.com.redhat:worker1")
    ... ; v4.detach_iscsi_client(vg, iqn); v4.delete_volume_group(vg); v4.delete_recovery_point(rp)

v4 conventions handled here: every mutating call carries a fresh NTNX-Request-Id (idempotency),
long operations return a task id polled at /api/prism/v4.0/config/tasks/{id}, deletes send the
entity's ETag as If-Match. Only objects named vmigrate-* are ever deleted.

VERIFY ON FIRST LIVE RUN: field names below follow the published v4.0 GA schemas
(dataprotection / volumes / clustermgmt / prism). They are isolated in this module, and every
response is checked for the fields we use, so a mismatch fails loudly with the raw payload.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from ...core.errors import ProviderError, SafetyGateError, TransientProviderError

PREFIX = "vmigrate-"
CLUSTERS = "/api/clustermgmt/v4.0/config/clusters"
RECOVERY_POINTS = "/api/dataprotection/v4.0/config/recovery-points"
VOLUME_GROUPS = "/api/volumes/v4.0/config/volume-groups"
TASKS = "/api/prism/v4.0/config/tasks"


def _need(d: Any, *path: str) -> Any:
    """Walk a v4 response; fail loudly (with the payload) when a field we rely on is absent."""
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            raise ProviderError(f"Prism v4 response lacks {'.'.join(path)!r} (API version mismatch?): {str(d)[:600]}")
        cur = cur[k]
    return cur


def recovery_point_body(vm_ext: str, name: str, app_consistent: bool, keep_hours: int = 24) -> dict[str, Any]:
    if not name.startswith(PREFIX):
        raise SafetyGateError("recovery point names must start with vmigrate-")
    exp = (datetime.now(timezone.utc) + timedelta(hours=keep_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"name": name, "expirationTime": exp,
            "recoveryPointType": "APPLICATION_CONSISTENT" if app_consistent else "CRASH_CONSISTENT",
            "vmRecoveryPoints": [{"vmExtId": vm_ext}]}


def volume_group_body(name: str, cluster_ext: str) -> dict[str, Any]:
    if not name.startswith(PREFIX):
        raise SafetyGateError("volume group names must start with vmigrate-")
    return {"name": name, "description": "vmigrate temporary read volume group (safe to delete)",
            "clusterReference": cluster_ext, "sharingStatus": "SHARED", "targetPrefix": name,
            "usageType": "USER"}


def vg_disk_body(rp_ext: str, disk_rp_ext: str, index: int) -> dict[str, Any]:
    return {"index": index, "description": f"vmigrate read copy of disk recovery point {disk_rp_ext}",
            "diskDataSourceReference": {"extId": disk_rp_ext, "entityType": "DISK_RECOVERY_POINT",
                                        "name": rp_ext}}


class PrismV4:
    def __init__(self, session: Any, pc_url: str, *, timeout: float = 60, task_timeout: float = 3600,
                 poll: float = 2, sleep: Any = time.sleep):
        self.s, self.base = session, pc_url.rstrip("/")
        self.timeout, self.task_timeout, self.poll, self.sleep = timeout, task_timeout, poll, sleep

    # ------------------------------------------------------------------ plumbing
    def _call(self, method: str, path: str, *, body: Any = None, etag: str | None = None) -> tuple[Any, Any]:
        headers = {"Accept": "application/json"}
        if method != "GET":
            headers["NTNX-Request-Id"] = str(uuid.uuid4())
        if etag:
            headers["If-Match"] = etag
        try:
            r = self.s.request(method, self.base + path, json=body, headers=headers, timeout=self.timeout)
        except Exception as e:  # noqa: BLE001
            raise TransientProviderError(f"Prism v4 {method} {path}: {e}") from e
        if r.status_code in (429,) or r.status_code >= 500:
            raise TransientProviderError(f"Prism v4 {method} {path}: HTTP {r.status_code} {r.text[:400]}")
        if r.status_code == 404 and method == "GET" and path == CLUSTERS:   # the v4 probe call
            raise ProviderError(f"Prism v4 API not available at {self.base} ({path} -> 404); needs Prism Central "
                                ">= 7.3 (pc.2024.3+). Use transport: nfs for older clusters.")
        if r.status_code >= 400:
            raise ProviderError(f"Prism v4 {method} {path}: HTTP {r.status_code} {r.text[:600]}")
        return (r.json() if r.content else {}), r.headers

    def _get(self, path: str) -> tuple[Any, str | None]:
        data, headers = self._call("GET", path)
        return data, (headers or {}).get("ETag") or (headers or {}).get("Etag")

    def _task(self, resp: Any, what: str) -> dict[str, Any]:
        tid = _need(resp, "data", "extId")
        deadline = time.monotonic() + self.task_timeout
        while True:
            t, _ = self._get(f"{TASKS}/{tid}")
            st = str(_need(t, "data", "status")).upper()
            if st == "SUCCEEDED":
                return t["data"]
            if st in ("FAILED", "CANCELED", "CANCELLED"):
                msgs = t["data"].get("errorMessages") or t["data"].get("legacyErrorMessage") or st
                raise ProviderError(f"{what} failed: {msgs}")
            if time.monotonic() > deadline:
                raise TransientProviderError(f"{what}: task {tid} still {st} after {self.task_timeout}s")
            self.sleep(self.poll)

    @staticmethod
    def _affected(task: dict[str, Any], rel_contains: str) -> str:
        for e in task.get("entitiesAffected") or []:
            if rel_contains in str(e.get("rel", "")):
                return e["extId"]
        raise ProviderError(f"task did not report a created {rel_contains}: {str(task)[:500]}")

    # ------------------------------------------------------------------ clusters
    def clusters(self) -> list[dict[str, Any]]:
        data, _ = self._get(CLUSTERS)
        return data.get("data") or []

    def data_services_ip(self, cluster_ext: str) -> str | None:
        for c in self.clusters():
            if c.get("extId") == cluster_ext:
                ip = (((c.get("network") or {}).get("externalDataServiceIp") or {}).get("ipv4") or {}).get("value")
                return ip
        return None

    def pc_version(self) -> str | None:
        for c in self.clusters():
            if "PRISM_CENTRAL" in ((c.get("config") or {}).get("clusterFunction") or []):
                return ((c.get("config") or {}).get("buildInfo") or {}).get("version")
        return None

    # ------------------------------------------------------------------ recovery points
    def create_recovery_point(self, vm_ext: str, name: str, *, app_consistent: bool) -> str:
        resp, _ = self._call("POST", RECOVERY_POINTS, body=recovery_point_body(vm_ext, name, app_consistent))
        task = self._task(resp, f"recovery point {name}")
        return self._affected(task, "recovery-point")

    def recovery_point_disks(self, rp_ext: str) -> dict[str, str]:
        data, _ = self._get(f"{RECOVERY_POINTS}/{rp_ext}")
        out: dict[str, str] = {}
        for vrp in _need(data, "data", "vmRecoveryPoints"):
            for d in vrp.get("diskRecoveryPoints") or []:
                out[_need(d, "diskExtId")] = _need(d, "diskRecoveryPointExtId")
        return out

    def delete_recovery_point(self, rp_ext: str) -> None:
        data, etag = self._get(f"{RECOVERY_POINTS}/{rp_ext}")
        name = str((data.get("data") or {}).get("name", ""))
        if not name.startswith(PREFIX):
            raise SafetyGateError(f"refusing to delete recovery point {rp_ext} ({name!r}): not created by vmigrate")
        resp, _ = self._call("DELETE", f"{RECOVERY_POINTS}/{rp_ext}", etag=etag)
        if resp:
            self._task(resp, f"delete recovery point {name}")

    # ------------------------------------------------------------------ volume groups
    def create_volume_group(self, name: str, cluster_ext: str) -> str:
        resp, _ = self._call("POST", VOLUME_GROUPS, body=volume_group_body(name, cluster_ext))
        return self._affected(self._task(resp, f"volume group {name}"), "volume-group")

    def volume_group(self, vg_ext: str) -> dict[str, Any]:
        data, _ = self._get(f"{VOLUME_GROUPS}/{vg_ext}")
        return _need(data, "data")

    def add_disk_from_recovery_point(self, vg_ext: str, rp_ext: str, disk_rp_ext: str, index: int) -> None:
        resp, _ = self._call("POST", f"{VOLUME_GROUPS}/{vg_ext}/disks", body=vg_disk_body(rp_ext, disk_rp_ext, index))
        self._task(resp, f"volume group disk {index}")

    def vg_disks(self, vg_ext: str) -> list[dict[str, Any]]:
        data, _ = self._get(f"{VOLUME_GROUPS}/{vg_ext}/disks")
        return [{"ext": _need(d, "extId"), "index": int(_need(d, "index")), "size": int(d.get("diskSizeBytes") or 0)}
                for d in data.get("data") or []]

    def attach_iscsi_client(self, vg_ext: str, iqn: str) -> None:
        resp, _ = self._call("POST", f"{VOLUME_GROUPS}/{vg_ext}/$actions/attach-iscsi-client",
                             body={"iscsiInitiatorName": iqn})
        self._task(resp, "attach iSCSI client")

    def detach_iscsi_client(self, vg_ext: str, iqn: str) -> None:
        resp, _ = self._call("POST", f"{VOLUME_GROUPS}/{vg_ext}/$actions/detach-iscsi-client",
                             body={"iscsiInitiatorName": iqn})
        self._task(resp, "detach iSCSI client")

    def delete_volume_group(self, vg_ext: str) -> None:
        vg = self.volume_group(vg_ext)
        if not str(vg.get("name", "")).startswith(PREFIX):
            raise SafetyGateError(f"refusing to delete volume group {vg_ext} ({vg.get('name')!r}): not vmigrate's")
        _, etag = self._get(f"{VOLUME_GROUPS}/{vg_ext}")
        resp, _ = self._call("DELETE", f"{VOLUME_GROUPS}/{vg_ext}", etag=etag)
        if resp:
            self._task(resp, f"delete volume group {vg.get('name')}")
