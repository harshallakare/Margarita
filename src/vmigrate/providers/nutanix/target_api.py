"""Purpose: the Prism calls the Nutanix TARGET needs, in one place, as plain dicts (tests use a fake).
Volume groups (v4, same API the iSCSI source uses live) hold the disks while they are written over
iSCSI; VMs are created and powered with Prism Element v2, cloning their disks from the volume-group
disks (vm_disk_clone: instant, copy-on-write).

Usage:
    api = NutanixTargetApi(PrismClient(...), PrismV4(...))
    vg = api.create_volume_group("vmigrate-disk-web01-disk00", cluster_ext)
    api.add_empty_disk(vg, size_bytes, container_uuid); api.vg_disks(vg)
    uuid = api.create_vm(body); api.power(uuid, "ON"); api.vm(uuid); api.delete_vm(uuid)

VERIFY ON FIRST LIVE RUN (not exercised on a real cluster yet; each is isolated here):
  * v4 volume-group disk create body for an EMPTY disk: {"index", "diskSizeBytes", "storageContainerId"}
  * a v4 volume-group disk's extId is the vmdisk_uuid v2 `vm_disk_clone.disk_address.vmdisk_uuid` takes
  * v2 VM create with "boot": {"uefi_boot", "secure_boot"} and "machine_type": "Q35" for secure boot
"""

from __future__ import annotations

import time
from typing import Any

from ...core import cancel
from ...core.errors import ProviderError, SafetyGateError, TransientProviderError
from .v4 import PREFIX, VOLUME_GROUPS, PrismV4, _need


class NutanixTargetApi:
    def __init__(self, client: Any, v4: PrismV4, *, task_timeout: float = 3600, poll: float = 3):
        self.c, self.v4 = client, v4
        self.task_timeout, self.poll = task_timeout, poll

    # ------------------------------------------------------------------ cluster facts
    def cluster_uuid(self) -> str:
        return str((self.c.v2_get("/cluster") or {})["uuid"])

    def data_services_ip(self, cluster_ext: str) -> str | None:
        return self.v4.data_services_ip(cluster_ext)

    def containers(self) -> list[dict[str, Any]]:
        return [{"uuid": c["storage_container_uuid"], "name": c["name"],
                 "free": int((c.get("usage_stats") or {}).get("storage.user_free_bytes") or 0)}
                for c in (self.c.v2_get("/storage_containers") or {}).get("entities") or []]

    def networks(self) -> list[dict[str, Any]]:
        return [{"uuid": n["uuid"], "name": n.get("name") or n["uuid"], "vlan": n.get("vlan_id")}
                for n in (self.c.v2_get("/networks") or {}).get("entities") or []]

    # ------------------------------------------------------------------ volume groups (v4)
    def create_volume_group(self, name: str, cluster_ext: str) -> str:
        return self.v4.create_volume_group(name, cluster_ext)

    def find_volume_group(self, name: str) -> str | None:
        return self.v4.find_by_name("volume-group", name)

    def volume_group(self, vg: str) -> dict[str, Any] | None:
        try:
            return self.v4.volume_group(vg)
        except ProviderError as e:
            if "404" in str(e):
                return None
            raise

    def add_empty_disk(self, vg: str, size_bytes: int, container_uuid: str, index: int = 0) -> None:
        body = {"index": index, "diskSizeBytes": size_bytes, "storageContainerId": container_uuid,
                "description": "vmigrate target disk"}
        resp, _ = self.v4._call("POST", f"{VOLUME_GROUPS}/{vg}/disks", body=body)
        self.v4._task(resp, f"volume group {vg} disk")

    def vg_disks(self, vg: str) -> list[dict[str, Any]]:
        return self.v4.vg_disks(vg)

    def attach_iscsi_client(self, vg: str, iqn: str) -> None:
        self.v4.attach_iscsi_client(vg, iqn)

    def detach_iscsi_client(self, vg: str, iqn: str) -> None:
        self.v4.detach_iscsi_client(vg, iqn)

    def delete_volume_group(self, vg: str) -> None:
        self.v4.delete_volume_group(vg)          # refuses names that do not start with vmigrate-

    # ------------------------------------------------------------------ VMs (Prism Element v2)
    def _wait_task(self, resp: Any, what: str) -> dict[str, Any]:
        tid = _need(resp, "task_uuid")
        deadline = time.monotonic() + self.task_timeout
        while True:
            t = self.c.v2_get(f"/tasks/{tid}") or {}
            st = str(t.get("progress_status", "")).lower()
            if st == "succeeded":
                return t
            if st in ("failed", "aborted"):
                raise ProviderError(f"{what} failed: {t.get('meta_response') or t.get('message') or st}")
            if time.monotonic() > deadline:
                raise TransientProviderError(f"{what}: task {tid} still {st} after {self.task_timeout}s")
            cancel.sleep(self.poll)

    def create_vm(self, body: dict[str, Any]) -> str:
        t = self._wait_task(self.c.v2_post("/vms", body), f"create VM {body.get('name')}")
        ents = [e.get("entity_id") for e in t.get("entity_list") or [] if e.get("entity_type", "VM").upper() == "VM"]
        if ents and ents[0]:
            return str(ents[0])
        hits = self.vms_by_name(body["name"])
        if len(hits) == 1:
            return str(hits[0]["uuid"])
        raise ProviderError(f"VM {body['name']!r} created but its UUID could not be determined: {str(t)[:800]}")

    def vm(self, uuid: str) -> dict[str, Any] | None:
        try:
            return self.c.v2_get(f"/vms/{uuid}", params={"include_vm_disk_config": "true",
                                                          "include_vm_nic_config": "true"})
        except ProviderError as e:
            if "404" in str(e) or "ENTITY_NOT_FOUND" in str(e):
                return None
            raise

    def vms_by_name(self, name: str) -> list[dict[str, Any]]:
        res = self.c.v2_get("/vms", params={"filter": f"vm_name=={name}"}) or {}
        return [v for v in res.get("entities") or [] if v.get("name") == name]

    def attach_disk(self, uuid: str, spec: dict[str, Any]) -> None:
        self._wait_task(self.c.v2_post(f"/vms/{uuid}/disks/attach", {"vm_disks": [spec]}), f"attach disk to {uuid}")

    def power(self, uuid: str, transition: str) -> None:
        self._wait_task(self.c.v2_post(f"/vms/{uuid}/set_power_state", {"transition": transition}),
                        f"power {transition} {uuid}")

    def delete_vm(self, uuid: str) -> None:
        self._wait_task(self.c.v2_delete(f"/vms/{uuid}"), f"delete VM {uuid}")


def ensure_ours(vg_info: dict[str, Any] | None, vg: str) -> None:
    if vg_info is not None and not str(vg_info.get("name", "")).startswith(PREFIX):
        raise SafetyGateError(f"refusing to touch volume group {vg} ({vg_info.get('name')!r}): not vmigrate's")
