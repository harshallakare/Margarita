"""Purpose: the narrow slice of OpenStack the VHI target needs, behind one class so the target logic
can be unit-tested with a fake. Only this file touches openstacksdk objects.

Usage: api = OpenStackApi(auth.connect(cfg)); api.create_volume(name, gib, vtype, metadata)
All returned objects are plain dicts.
"""

from __future__ import annotations

from typing import Any


def _d(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    return obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)


class OpenStackApi:
    def __init__(self, conn: Any):
        self.conn = conn

    # identity
    def current_project_id(self) -> str:
        return self.conn.current_project_id

    # block storage
    def find_volume_type(self, name: str) -> dict[str, Any] | None:
        t = self.conn.block_storage.find_type(name, ignore_missing=True)
        return _d(t) if t else None

    def create_volume(self, name: str, size_gib: int, volume_type: str, metadata: dict[str, str],
                      availability_zone: str | None = None, source_volid: str | None = None) -> dict[str, Any]:
        kw: dict[str, Any] = dict(name=name, size=size_gib, volume_type=volume_type, metadata=metadata)
        if availability_zone:
            kw["availability_zone"] = availability_zone
        if source_volid:
            kw["source_volume_id"] = source_volid   # Cinder clone (deploy from a replica volume)
        return _d(self.conn.block_storage.create_volume(**kw))

    def get_volume(self, vol_id: str) -> dict[str, Any]:
        return _d(self.conn.block_storage.get_volume(vol_id))

    def volumes_by_name(self, name: str) -> list[dict[str, Any]]:
        return [_d(v) for v in self.conn.block_storage.volumes(details=True, name=name)]

    def delete_volume(self, vol_id: str) -> None:
        self.conn.block_storage.delete_volume(vol_id, ignore_missing=True)

    def volume_action(self, vol_id: str, body: dict[str, Any]) -> None:
        self.conn.block_storage.post(f"/volumes/{vol_id}/action", json=body)

    # compute
    def flavors(self) -> list[dict[str, Any]]:
        return [_d(f) for f in self.conn.compute.flavors(details=True)]

    def find_flavor(self, name_or_id: str) -> dict[str, Any] | None:
        f = self.conn.compute.find_flavor(name_or_id, ignore_missing=True)
        return _d(f) if f else None

    def create_server(self, **kw: Any) -> dict[str, Any]:
        return _d(self.conn.compute.create_server(**kw))

    def get_server(self, server_id: str) -> dict[str, Any]:
        return _d(self.conn.compute.get_server(server_id))

    def find_server(self, name: str) -> dict[str, Any] | None:
        s = self.conn.compute.find_server(name, ignore_missing=True)
        return _d(s) if s else None

    def delete_server(self, server_id: str) -> None:
        self.conn.compute.delete_server(server_id, ignore_missing=True)

    def attach_volume(self, server_id: str, vol_id: str) -> None:
        self.conn.compute.create_volume_attachment(server_id, volume_id=vol_id)

    def attachments(self, server_id: str) -> list[dict[str, Any]]:
        return [_d(a) for a in self.conn.compute.volume_attachments(server_id)]

    def console_log(self, server_id: str, length: int = 300) -> str:
        out = self.conn.compute.get_server_console_output(server_id, length=length)
        return (out or {}).get("output", "") if isinstance(out, dict) else str(out)

    # network
    def networks(self) -> list[dict[str, Any]]:
        return [_d(n) for n in self.conn.network.networks()]

    def volume_types(self) -> list[dict[str, Any]]:
        return [_d(t) for t in self.conn.block_storage.types()]

    def find_network(self, name_or_id: str) -> dict[str, Any] | None:
        n = self.conn.network.find_network(name_or_id, ignore_missing=True)
        return _d(n) if n else None

    def create_port(self, **kw: Any) -> dict[str, Any]:
        return _d(self.conn.network.create_port(**kw))

    def ports_by_name(self, name: str) -> list[dict[str, Any]]:
        return [_d(p) for p in self.conn.network.ports(name=name)]

    def delete_port(self, port_id: str) -> None:
        self.conn.network.delete_port(port_id, ignore_missing=True)
