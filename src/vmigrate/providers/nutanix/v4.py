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

Changed regions (CRT, for incremental replica syncs), wire names taken from Nutanix's own v4
dataprotection SDK (ntnx-dataprotection-py-client 4.4.2, read not vendored):
    1. PC   POST {dp}/config/recovery-points/{rp}/$actions/discover-cluster
            {"operation": "COMPUTE_CHANGED_REGIONS", "spec": {"$objectType":
             "dataprotection.v4.content.ComputeChangedRegionsClusterDiscoverSpec",
             "diskRecoveryPoint": {VmDiskRecoveryPointReference}, "referenceDiskRecoveryPoint": {...}}}
            -> data.clusterIp.ipv4.value + data.jwtToken (15 min, bound to that disk recovery point)
    2. PE   POST https://<clusterIp>:9440{dp}/content/recovery-points/{rp}/vm-recovery-points/{vmrp}/
            disk-recovery-points/{drp}/$actions/compute-changed-regions   (Cookie NTNX_IGW_SESSION=<jwt>)
            {"offset", "referenceRecoveryPointExtId", "referenceVmRecoveryPointExtId",
             "referenceDiskRecoveryPointExtId"} -> data: [{offset, length, regionType: REGULAR|ZEROED}],
            metadata.extraInfo: [{name: nextOffset}, {name: fileSize}]; up to 10,000 regions per call.

VERIFY ON FIRST LIVE RUN: field names below follow the published v4.0 GA schemas
(dataprotection / volumes / clustermgmt / prism). They are isolated in this module, and every
response is checked for the fields we use, so a mismatch fails loudly with the raw payload.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from urllib.parse import quote
from datetime import datetime, timedelta, timezone
from typing import Any

from ...core.errors import ProviderError, SafetyGateError, TransientProviderError

log = logging.getLogger("vmigrate.nutanix.v4")

PREFIX = "vmigrate-"
CLUSTERS = "/api/clustermgmt/v4.0/config/clusters"
RECOVERY_POINTS = "/api/dataprotection/v4.0/config/recovery-points"
DP_CONTENT = "/api/dataprotection/{ver}/content/recovery-points"
DP_CONFIG = "/api/dataprotection/{ver}/config/recovery-points"
OT = "dataprotection.v4.content."
VOLUME_GROUPS = "/api/volumes/v4.0/config/volume-groups"
ISCSI_CLIENTS = "/api/volumes/v4.0/config/iscsi-clients"
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


def _disk_ref(r: dict[str, str]) -> dict[str, Any]:
    return {"$objectType": OT + "VmDiskRecoveryPointReference", "recoveryPointExtId": r["recovery_point"],
            "vmRecoveryPointExtId": r["vm_recovery_point"], "diskRecoveryPointExtId": r["disk_recovery_point"]}


def discover_body(cur: dict[str, str], ref: dict[str, str] | None) -> dict[str, Any]:
    spec: dict[str, Any] = {"$objectType": OT + "ComputeChangedRegionsClusterDiscoverSpec",
                            "diskRecoveryPoint": _disk_ref(cur)}
    if ref:
        spec["referenceDiskRecoveryPoint"] = _disk_ref(ref)
    return {"operation": "COMPUTE_CHANGED_REGIONS", "spec": spec}


def compute_body(offset: int, ref: dict[str, str] | None) -> dict[str, Any]:
    b: dict[str, Any] = {"$objectType": OT + "VmRecoveryPointChangedRegionsComputeSpec", "offset": offset}
    if ref:
        b.update(referenceRecoveryPointExtId=ref["recovery_point"],
                 referenceVmRecoveryPointExtId=ref["vm_recovery_point"],
                 referenceDiskRecoveryPointExtId=ref["disk_recovery_point"])
    return b


def _extra(meta: Any, name: str) -> str | None:
    for kv in (meta or {}).get("extraInfo") or []:
        if kv.get("name") == name:
            v = kv.get("value")
            return str(v.get("value") if isinstance(v, dict) else v)
    return None


class PrismV4:
    def __init__(self, session: Any, pc_url: str, *, timeout: float = 60, task_timeout: float = 3600,
                 poll: float = 2, sleep: Any = time.sleep, crt_version: str = "v4.0"):
        self.crt_version = crt_version
        self.s, self.base = session, pc_url.rstrip("/")
        self.timeout, self.task_timeout, self.poll, self.sleep = timeout, task_timeout, poll, sleep

    # ------------------------------------------------------------------ plumbing
    def _call(self, method: str, path: str, *, body: Any = None, etag: str | None = None,
              quiet: bool = False) -> tuple[Any, Any]:
        headers = {"Accept": "application/json"}
        if method != "GET":
            headers["NTNX-Request-Id"] = str(uuid.uuid4())
        if etag:
            headers["If-Match"] = etag
        t0 = time.monotonic()
        try:
            r = self.s.request(method, self.base + path, json=body, headers=headers, timeout=self.timeout)
        except Exception as e:  # noqa: BLE001
            log.warning("Prism v4 %s %s failed after %.1fs: %s", method, path, time.monotonic() - t0, e)
            raise TransientProviderError(f"Prism v4 {method} {path}: {e}") from e
        log.debug("Prism v4 %s %s -> %s in %.2fs", method, path, r.status_code, time.monotonic() - t0)
        if r.status_code >= 400:   # quiet: an expected miss while probing (alternate API version, gone object)
            (log.debug if quiet else log.warning)("Prism v4 %s %s -> HTTP %s: %s", method, path, r.status_code,
                                                  r.text[:500])
        if r.status_code in (429,) or r.status_code >= 500:
            raise TransientProviderError(f"Prism v4 {method} {path}: HTTP {r.status_code} {r.text[:400]}")
        if r.status_code == 404 and method == "GET" and path == CLUSTERS:   # the v4 probe call
            raise ProviderError(f"Prism v4 API not available at {self.base} ({path} -> 404); needs Prism Central "
                                ">= 7.3 (pc.2024.3+). Use transport: nfs for older clusters.")
        if r.status_code >= 400:
            raise ProviderError(f"Prism v4 {method} {path}: HTTP {r.status_code} {r.text[:600]}")
        return (r.json() if r.content else {}), r.headers

    def _get(self, path: str, *, quiet: bool = False) -> tuple[Any, str | None]:
        data, headers = self._call("GET", path, quiet=quiet)
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
    def _affected(task: dict[str, Any], kind: str) -> str | None:
        """ID of the object a v4 task created. Prism reports it in entitiesAffected (rel spelled e.g.
        dataprotection:config:recovery-point) or only in completionDetails, depending on the
        version; seen live: a CreateRecoveryPoint task listing just the VM. None when absent."""
        want = re.sub(r"[^a-z]", "", kind.lower())                       # "recoverypoint"
        for e in task.get("entitiesAffected") or []:
            if want in re.sub(r"[^a-z]", "", str(e.get("rel", "")).lower()) and e.get("extId"):
                return str(e["extId"])
        for d in task.get("completionDetails") or []:
            n = re.sub(r"[^a-z]", "", str(d.get("name", "")).lower())
            v = d.get("value")
            v = v.get("value") if isinstance(v, dict) else v
            if v and isinstance(v, str) and (want in n or n in ("extid", "entityextid")):
                return v
        return None

    def _created(self, task: dict[str, Any], kind: str, collection: str, name: str) -> str:
        """The created object's ID from the task, else by its unique vmigrate- name."""
        got = self._affected(task, kind)
        if got:
            return got
        log.info("task for %s %s did not report its ID; looking it up by name", kind, name)
        hits = [x for x in self._list_named(collection, name) if x.get("name") == name and x.get("extId")]
        if len(hits) == 1:
            return str(hits[0]["extId"])
        raise ProviderError(f"{kind} {name!r} was created but its ID could not be determined "
                            f"({len(hits)} objects with that name); task: {str(task)[:1500]}")

    def find_by_name(self, kind: str, name: str) -> str | None:
        """ID of the vmigrate-created recovery point / volume group with exactly this name, else None."""
        if not name.startswith(PREFIX):
            raise SafetyGateError(f"refusing to look up {name!r}: not a vmigrate name")
        coll = {"recovery-point": RECOVERY_POINTS, "volume-group": VOLUME_GROUPS}[kind]
        hits = [x for x in self._list_named(coll, name) if x.get("name") == name and x.get("extId")]
        return str(hits[0]["extId"]) if len(hits) == 1 else None

    def _list_named(self, collection: str, name: str) -> list[dict[str, Any]]:
        try:
            flt = quote(f"name eq '{name}'", safe="'=")
            data, _ = self._get(f"{collection}?$filter={flt}", quiet=True)
            return data.get("data") or []
        except ProviderError:                    # $filter not supported here: scan the first page
            data, _ = self._get(f"{collection}?$limit=100")
            return data.get("data") or []

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
        return self._created(task, "recovery-point", RECOVERY_POINTS, name)

    def recovery_point_layout(self, rp_ext: str) -> dict[str, Any]:
        """{vm_recovery_point, disks: {vm disk ext: disk recovery point ext}, unmapped: [disk RP ext]}
        for a single-VM recovery point. `diskExtId` is optional in the v4 model (seen live: absent);
        when missing, the VM-recovery-point endpoint and newer API versions are asked, and whatever
        is still unknown is returned in `unmapped` (in Prism's order) for the caller to resolve."""
        data, _ = self._get(f"{RECOVERY_POINTS}/{rp_ext}")
        vrps = _need(data, "data", "vmRecoveryPoints")
        if len(vrps) != 1:
            raise ProviderError(f"recovery point {rp_ext} has {len(vrps)} VM recovery points, expected 1")
        vmrp = _need(vrps[0], "extId")
        entries = [(d.get("diskExtId"), _need(d, "diskRecoveryPointExtId")) for d in vrps[0].get("diskRecoveryPoints") or []]
        if any(not de for de, _ in entries):
            log.info("recovery point %s: diskExtId missing in %s; trying other endpoints", rp_ext, RECOVERY_POINTS)
            known: dict[str, str] = {}
            alt = [f"{RECOVERY_POINTS}/{rp_ext}/vm-recovery-points/{vmrp}"]
            alt += [f"{RECOVERY_POINTS.replace('/v4.0/', f'/{v}/')}/{rp_ext}" for v in ("v4.2", "v4.1")]
            for path in alt:
                try:
                    d2, _ = self._get(path, quiet=True)
                except ProviderError as e:
                    log.debug("  %s: %s", path, e)
                    continue
                body = (d2 or {}).get("data") or {}
                vr = body.get("vmRecoveryPoints") or [body]
                for v in vr:
                    for d in v.get("diskRecoveryPoints") or []:
                        if d.get("diskExtId") and d.get("diskRecoveryPointExtId"):
                            known[d["diskRecoveryPointExtId"]] = d["diskExtId"]
                if all(known.get(drp) or de for de, drp in entries):
                    log.info("  disk mapping found via %s", path)
                    break
            entries = [(de or known.get(drp), drp) for de, drp in entries]
        return {"vm_recovery_point": vmrp,
                "disks": {de: drp for de, drp in entries if de},
                "unmapped": [drp for de, drp in entries if not de]}

    def set_expiration(self, rp_ext: str, hours: float) -> None:
        """Keep a recovery point as the next sync's CRT reference (created with a 24 h expiry)."""
        data, etag = self._get(f"{RECOVERY_POINTS}/{rp_ext}")
        if not str((data.get("data") or {}).get("name", "")).startswith(PREFIX):
            raise SafetyGateError(f"refusing to change recovery point {rp_ext}: not created by vmigrate")
        exp = (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        resp, _ = self._call("POST", f"{RECOVERY_POINTS}/{rp_ext}/$actions/set-expiration-time",
                             body={"expirationTime": exp}, etag=etag)
        if resp:
            self._task(resp, f"extend recovery point {rp_ext}")

    def _discover(self, cur: dict[str, str], ref: dict[str, str] | None) -> tuple[str, str]:
        path = DP_CONFIG.format(ver=self.crt_version) + f"/{cur['recovery_point']}/$actions/discover-cluster"
        data, _ = self._call("POST", path, body=discover_body(cur, ref))
        return str(_need(data, "data", "clusterIp", "ipv4", "value")), str(_need(data, "data", "jwtToken"))

    def changed_regions(self, cur: dict[str, str], ref: dict[str, str] | None, size: int,
                        *, max_pages: int = 100000) -> list[tuple[int, int, str]]:
        """All regions of `cur` that differ from `ref` (None: everything allocated), as
        (offset, length, REGULAR|ZEROED). Pages with nextOffset; re-discovers when the JWT expires."""
        ip, jwt = self._discover(cur, ref)
        path = (DP_CONTENT.format(ver=self.crt_version) + f"/{cur['recovery_point']}/vm-recovery-points/"
                f"{cur['vm_recovery_point']}/disk-recovery-points/{cur['disk_recovery_point']}"
                "/$actions/compute-changed-regions")
        out: list[tuple[int, int, str]] = []
        off, retried = 0, False
        for _ in range(max_pages):
            headers = {"Accept": "application/json", "Content-Type": "application/json",
                       "NTNX-Request-Id": str(uuid.uuid4()), "Cookie": f"NTNX_IGW_SESSION={jwt}"}
            try:
                r = self.s.request("POST", f"https://{ip}:9440{path}", json=compute_body(off, ref), headers=headers,
                                   timeout=self.timeout)
            except Exception as e:  # noqa: BLE001
                raise TransientProviderError(f"CRT {ip}: {e}") from e
            if r.status_code == 401 and not retried:          # JWT expired (15 min): get a fresh one once
                ip, jwt = self._discover(cur, ref)
                retried = True
                continue
            if r.status_code >= 500 or r.status_code == 429:
                raise TransientProviderError(f"CRT {ip}: HTTP {r.status_code} {r.text[:300]}")
            if r.status_code >= 400:
                raise ProviderError(f"CRT {ip}: HTTP {r.status_code} {r.text[:600]}")
            retried = False
            body = r.json() if r.content else {}
            for reg in body.get("data") or []:
                out.append((int(_need(reg, "offset")), int(_need(reg, "length")),
                            str(reg.get("regionType") or "REGULAR")))
            nxt = _extra(body.get("metadata"), "nextOffset")
            n = int(nxt) if nxt not in (None, "", "None") else 0
            if n <= off or n >= size:
                return out
            off = n
        raise ProviderError(f"CRT: more than {max_pages} pages for disk recovery point {cur['disk_recovery_point']}")

    def recovery_point_disks(self, rp_ext: str) -> dict[str, str]:
        data, _ = self._get(f"{RECOVERY_POINTS}/{rp_ext}")
        out: dict[str, str] = {}
        for vrp in _need(data, "data", "vmRecoveryPoints"):
            for d in vrp.get("diskRecoveryPoints") or []:
                out[_need(d, "diskExtId")] = _need(d, "diskRecoveryPointExtId")
        return out

    def _get_or_gone(self, path: str, what: str) -> tuple[Any, str | None] | None:
        """GET for a cleanup step; None when the object no longer exists (an earlier attempt already
        removed it; seen live: DP-10400 "does not exist in IDF"), so cleanup is safe to repeat."""
        try:
            return self._get(path, quiet=True)
        except ProviderError as e:
            if "HTTP 404" in str(e):
                log.info("%s already gone; nothing to clean up", what)
                return None
            raise

    def delete_recovery_point(self, rp_ext: str) -> None:
        got = self._get_or_gone(f"{RECOVERY_POINTS}/{rp_ext}", f"recovery point {rp_ext}")
        if got is None:
            return
        data, etag = got
        name = str((data.get("data") or {}).get("name", ""))
        if not name.startswith(PREFIX):
            raise SafetyGateError(f"refusing to delete recovery point {rp_ext} ({name!r}): not created by vmigrate")
        resp, _ = self._call("DELETE", f"{RECOVERY_POINTS}/{rp_ext}", etag=etag)
        if resp:
            self._task(resp, f"delete recovery point {name}")

    # ------------------------------------------------------------------ volume groups
    def create_volume_group(self, name: str, cluster_ext: str) -> str:
        resp, _ = self._call("POST", VOLUME_GROUPS, body=volume_group_body(name, cluster_ext))
        return self._created(self._task(resp, f"volume group {name}"), "volume-group", VOLUME_GROUPS, name)

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
        """Detach takes the iSCSI client's UUID, not its IQN (seen live: VOL-40101 "the iSCSI client
        UUID must be specified"). The UUID comes from the VG's iscsi-client-attachments, else from the
        iscsi-clients list by initiator name. Only ever acts on a vmigrate- volume group."""
        got = self._get_or_gone(f"{VOLUME_GROUPS}/{vg_ext}", f"volume group {vg_ext}")
        if got is None:
            return
        vg = _need(got[0], "data")
        if not str(vg.get("name", "")).startswith(PREFIX):
            raise SafetyGateError(f"refusing to detach clients from volume group {vg_ext} ({vg.get('name')!r})")
        ids: list[str] = []
        try:
            data, _ = self._get(f"{VOLUME_GROUPS}/{vg_ext}/iscsi-client-attachments")
            atts = [a for a in data.get("data") or [] if a.get("extId")]
            mine = [a for a in atts if iqn and iqn in str(a)]
            ids = [str(a["extId"]) for a in (mine or atts)]       # our VG: every client on it is ours
        except ProviderError as e:
            log.info("iscsi-client-attachments for %s not available (%s); looking the client up by IQN", vg_ext, e)
        if not ids and iqn:
            flt = quote(f"iscsiInitiatorName eq '{iqn}'", safe="'=")
            data, _ = self._get(f"{ISCSI_CLIENTS}?$filter={flt}")
            ids = [str(c["extId"]) for c in data.get("data") or []
                   if c.get("extId") and c.get("iscsiInitiatorName", iqn) == iqn]
        if not ids:
            log.info("volume group %s has no iSCSI client attached", vg_ext)
            return
        for cid in ids:
            resp, _ = self._call("POST", f"{VOLUME_GROUPS}/{vg_ext}/$actions/detach-iscsi-client",
                                 body={"extId": cid})
            self._task(resp, f"detach iSCSI client {cid}")

    def delete_volume_group(self, vg_ext: str) -> None:
        got = self._get_or_gone(f"{VOLUME_GROUPS}/{vg_ext}", f"volume group {vg_ext}")
        if got is None:
            return
        data, etag = got
        vg = _need(data, "data")
        if not str(vg.get("name", "")).startswith(PREFIX):
            raise SafetyGateError(f"refusing to delete volume group {vg_ext} ({vg.get('name')!r}): not vmigrate's")
        resp, _ = self._call("DELETE", f"{VOLUME_GROUPS}/{vg_ext}", etag=etag)
        if resp:
            self._task(resp, f"delete volume group {vg.get('name')}")
