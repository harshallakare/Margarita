"""Purpose: the narrow slice of Nova / Cinder / Glance the OpenStack source needs, returning plain
dicts for mapping.py. Only this module touches openstacksdk objects; auth comes from
providers/common/openstack_auth (project-ID scoping, env scrub — pitfall 7).

Usage:
    c = OsCloud(cfg)
    c.servers(); c.server(id); c.nova_attachments(id); c.volumes_for(id); c.image(id); c.interfaces(id)
    img = c.create_server_image(id, "vmigrate-abc")         # Nova createImage (quiesces with guest agent)
    c.create_volume_snapshot(vol_id, "vmigrate-abc-vdb")     # for volumes of image-backed servers
    v = c.volume_from(snapshot_id=..., name="vmigrate-...")  # attach transport
    c.attach(worker_id, v); c.detach(worker_id, v); c.delete_volume(v)

Every delete is guarded: only images/snapshots/volumes whose name starts with vmigrate- are deleted.
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Mapping
from typing import Any

from ...core.errors import ProviderError, SafetyGateError, TransientProviderError
from ..common.openstack_auth import connect

PREFIX = "vmigrate-"


def _d(o: Any) -> dict[str, Any]:
    return o.to_dict() if hasattr(o, "to_dict") else dict(o)


def worker_server_id(timeout: float = 5) -> str:
    """This worker's own Nova server id, from the metadata service (attach transport)."""
    try:
        with urllib.request.urlopen("http://169.254.169.254/openstack/latest/meta_data.json", timeout=timeout) as r:
            return json.load(r)["uuid"]
    except Exception as e:  # noqa: BLE001
        raise ProviderError(f"cannot read the metadata service ({e}); set worker_server_id, or use transport rbd") from e


class OsCloud:
    def __init__(self, cfg: Mapping[str, Any], *, poll: float = 3, timeout: float = 3600):
        self.conn = connect(cfg, "openstack source")
        self.poll, self.timeout = poll, timeout

    def _wait(self, get, ok: str, what: str, bad: tuple[str, ...] = ("error",)) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        while True:
            o = _d(get())
            st = str(o.get("status", "")).lower()
            if st == ok:
                return o
            if st in bad or st.startswith("error"):
                raise ProviderError(f"{what}: status {st}")
            if time.monotonic() > deadline:
                raise TransientProviderError(f"{what}: still {st} after {self.timeout}s")
            time.sleep(self.poll)

    # ------------------------------------------------------------------ inventory
    def project_id(self) -> str:
        return self.conn.current_project_id

    def servers(self) -> list[dict[str, Any]]:
        return [{"id": s.id, "name": s.name, "status": s.status} for s in self.conn.compute.servers(details=True)]

    def server(self, sid: str) -> dict[str, Any]:
        s = self.conn.compute.get_server(sid)
        flv = _d(s.flavor or {})
        img = s.image or {}
        return {"id": s.id, "name": s.name, "status": s.status,
                "flavor": {"vcpus": flv.get("vcpus"), "ram": flv.get("ram"), "disk": flv.get("disk"),
                           "ephemeral": flv.get("ephemeral"), "swap": flv.get("swap") or 0},
                "image_id": img.get("id") if isinstance(img, dict) else getattr(img, "id", None),
                "root_device_name": getattr(s, "root_device_name", None)}

    def nova_attachments(self, sid: str) -> list[dict[str, Any]]:
        return [{"volume_id": a.volume_id, "device": a.device} for a in self.conn.compute.volume_attachments(sid)]

    def volumes_for(self, sid: str) -> list[dict[str, Any]]:
        out = []
        for v in self.conn.block_storage.volumes(details=True):
            atts = [{"server_id": a.get("server_id"), "device": a.get("device")} for a in (v.attachments or [])]
            if any(a["server_id"] == sid for a in atts):
                out.append({"id": v.id, "name": v.name, "size": v.size, "bootable": str(v.is_bootable).lower() == "true",
                            "attachments": atts, "image_meta": dict(v.volume_image_metadata or {}), "status": v.status})
        return out

    def image(self, image_id: str) -> dict[str, Any]:
        i = _d(self.conn.image.get_image(image_id))
        props = dict(i.get("properties") or {})
        for k, v in i.items():       # sdk exposes known props as attributes too
            if k.startswith(("hw_", "os_")) and v is not None:
                props[k] = v
        return {"id": i["id"], "name": i.get("name"), "status": i.get("status"), "virtual_size": i.get("virtual_size"),
                "disk_format": i.get("disk_format"),
                "props": props, "bdm": json.loads(props.get("block_device_mapping") or "[]")
                if isinstance(props.get("block_device_mapping"), str) else (props.get("block_device_mapping") or []),
                "direct_url": i.get("direct_url"), "locations": i.get("locations") or []}

    def interfaces(self, sid: str) -> list[dict[str, Any]]:
        out = []
        for p in self.conn.compute.server_interfaces(sid):
            net = self.conn.network.get_network(p.net_id)
            out.append({"port_id": p.port_id, "mac": p.mac_addr, "network": net.name if net else p.net_id})
        return out

    # ------------------------------------------------------------------ snapshots
    def create_server_image(self, sid: str, name: str) -> dict[str, Any]:
        if not name.startswith(PREFIX):
            raise SafetyGateError("snapshot image names must start with vmigrate-")
        img = self.conn.compute.create_server_image(sid, name=name, metadata={"vmigrate": "read point"}, wait=False)
        iid = img.id if hasattr(img, "id") else img
        self._wait(lambda: self.conn.image.get_image(iid), "active", f"server image {name}", ("killed", "deleted"))
        return self.image(iid)

    def create_volume_snapshot(self, volume_id: str, name: str) -> str:
        if not name.startswith(PREFIX):
            raise SafetyGateError("snapshot names must start with vmigrate-")
        s = self.conn.block_storage.create_snapshot(volume_id=volume_id, name=name, force=True)
        self._wait(lambda: self.conn.block_storage.get_snapshot(s.id), "available", f"volume snapshot {name}")
        return s.id

    def snapshot_name(self, snap_id: str) -> str | None:
        s = self.conn.block_storage.find_snapshot(snap_id, ignore_missing=True)
        return s.name if s else None

    def delete_snapshot(self, snap_id: str, *, created_by_image: str | None = None) -> None:
        name = self.snapshot_name(snap_id)
        if name is None:
            return
        # Nova names createImage snapshots "snapshot for <image name>"
        if not (name.startswith(PREFIX) or (created_by_image and name == f"snapshot for {created_by_image}")):
            raise SafetyGateError(f"refusing to delete volume snapshot {snap_id} ({name!r}): not created by vmigrate")
        self.conn.block_storage.delete_snapshot(snap_id, ignore_missing=True)

    def delete_image(self, image_id: str) -> None:
        i = self.conn.image.find_image(image_id, ignore_missing=True)
        if i is None:
            return
        if not str(i.name).startswith(PREFIX):
            raise SafetyGateError(f"refusing to delete image {image_id} ({i.name!r}): not created by vmigrate")
        self.conn.image.delete_image(image_id, ignore_missing=True)

    # ------------------------------------------------------------------ attach transport
    def volume_from(self, *, name: str, size_gib: int, snapshot_id: str | None = None, image_id: str | None = None) -> str:
        if not name.startswith(PREFIX):
            raise SafetyGateError("temporary volume names must start with vmigrate-")
        kw: dict[str, Any] = {"name": name, "size": size_gib, "metadata": {"vmigrate": "temporary read volume"}}
        if snapshot_id:
            kw["snapshot_id"] = snapshot_id
        else:
            kw["image_id"] = image_id
        v = self.conn.block_storage.create_volume(**kw)
        self._wait(lambda: self.conn.block_storage.get_volume(v.id), "available", f"volume {name}")
        try:
            self.conn.block_storage.set_readonly(v.id, True)
        except Exception:  # noqa: BLE001 - policy may forbid it; we open the device O_RDONLY anyway
            pass
        return v.id

    def attach(self, server_id: str, volume_id: str) -> None:
        self.conn.compute.create_volume_attachment(server_id, volume_id=volume_id)
        self._wait(lambda: self.conn.block_storage.get_volume(volume_id), "in-use", f"attach {volume_id}")

    def detach(self, server_id: str, volume_id: str) -> None:
        self.conn.compute.delete_volume_attachment(server_id, volume_id, ignore_missing=True)
        self._wait(lambda: self.conn.block_storage.get_volume(volume_id), "available", f"detach {volume_id}")

    def delete_volume(self, volume_id: str) -> None:
        v = self.conn.block_storage.find_volume(volume_id, ignore_missing=True)
        if v is None:
            return
        if not str(v.name).startswith(PREFIX):
            raise SafetyGateError(f"refusing to delete volume {volume_id} ({v.name!r}): not created by vmigrate")
        self.conn.block_storage.delete_volume(volume_id, ignore_missing=True)
