"""Purpose: PURE mapping from Nova/Cinder/Glance descriptions (plain dicts from cloud.py) to the IR,
the two-API disk cross-check, device ordering, snapshot <-> disk matching and Ceph RBD names.

Usage:
    spec = to_vmspec(server, attachments, volumes, image_props, interfaces)
    crosscheck(server, attachments, volumes)
    rbd_volume_spec(cfg, volume_id, snapshot_id)      # rbd:volumes/volume-<v>@snapshot-<s>:conf=...:id=...

Shapes:
    server      {id, name, flavor: {vcpus, ram (MiB), disk (GiB), ephemeral, swap}, image_id|None,
                 root_device_name|None}
    attachments [{volume_id, device}]              (Nova os-volume_attachments)
    volumes     [{id, size (GiB), bootable, attachments: [{server_id, device}], image_meta: {...}}]  (Cinder)
    image_props Glance properties of the server's image (image-backed) or the boot volume's
                volume_image_metadata (boot-from-volume): hw_disk_bus, hw_scsi_model, hw_firmware_type,
                os_type, os_distro, hw_vif_model, hw_qemu_guest_agent
    interfaces  [{port_id, mac, network}]

Rules:
  * image-backed root disk + every attached Cinder volume; no cap
  * flavor EPHEMERAL disks are REFUSED: Nova snapshots never include them (data would be lost)
  * swap is not data: noted, not copied
  * disks ordered by device name (vda < vdb < ... < vdz < vdaa); boot = root_device_name or vda
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ...ir import CpuSpec, DiskBus, DiskSpec, Firmware, GuestOS, NicSpec, OsFamily, VmSpec

GIB = 1 << 30
_DEV = re.compile(r"^/dev/(?:vd|sd|hd|xvd)([a-z]+)$")


class OpenstackMappingError(ValueError):
    pass


def device_key(dev: str | None) -> tuple[int, str]:
    m = _DEV.match(dev or "")
    if not m:
        return (99, dev or "")
    return (len(m.group(1)), m.group(1))


def bus_and_controller(props: Mapping[str, Any]) -> tuple[DiskBus, str]:
    bus = str(props.get("hw_disk_bus") or "virtio").lower()
    if bus == "scsi":
        model = str(props.get("hw_scsi_model") or "lsilogic").lower()
        return DiskBus.SCSI, "virtio-scsi" if model == "virtio-scsi" else model
    return {"virtio": (DiskBus.VIRTIO, "virtio-blk"), "ide": (DiskBus.IDE, "ide"),
            "sata": (DiskBus.SATA, "ahci")}.get(bus, (DiskBus.VIRTIO, "virtio-blk"))


def guest_os(props: Mapping[str, Any]) -> GuestOS:
    t = str(props.get("os_type") or "").lower()
    distro = props.get("os_distro")
    if t == "windows" or str(distro or "").startswith("windows"):
        return GuestOS(OsFamily.WINDOWS, distro="windows", version=props.get("os_version"))
    if t == "linux" or distro:
        return GuestOS(OsFamily.LINUX, distro=distro, version=props.get("os_version"))
    return GuestOS(OsFamily.UNKNOWN)


def _attached(volumes: list[dict[str, Any]], server_id: str) -> list[dict[str, Any]]:
    return [v for v in volumes if any(a.get("server_id") == server_id for a in v.get("attachments") or [])]


def image_backed(server: Mapping[str, Any], attachments: list[dict[str, Any]]) -> bool:
    root = server.get("root_device_name") or "/dev/vda"
    return bool(server.get("image_id")) and not any(a.get("device") == root for a in attachments)


def crosscheck(server: Mapping[str, Any], attachments: list[dict[str, Any]], volumes: list[dict[str, Any]]) -> dict[str, int]:
    root = 1 if image_backed(server, attachments) else 0
    return {"nova.os-volume_attachments": len(attachments) + root,
            "cinder.volumes.attachments": len(_attached(volumes, server["id"])) + root}


def to_vmspec(server: Mapping[str, Any], attachments: list[dict[str, Any]], volumes: list[dict[str, Any]],
              image_props: Mapping[str, Any], interfaces: list[dict[str, Any]],
              image_virtual_size: int | None = None) -> VmSpec:
    name = server["name"]
    flv = server.get("flavor") or {}
    if int(flv.get("ephemeral") or 0) > 0:
        raise OpenstackMappingError(f"{name}: flavor has a {flv['ephemeral']} GiB EPHEMERAL disk; Nova snapshots "
                                    "never include it, so its data would be lost. Move the data to a volume first.")
    by_id = {v["id"]: v for v in volumes}
    rows: list[dict[str, Any]] = []
    root_dev = server.get("root_device_name") or "/dev/vda"
    if image_backed(server, attachments):
        size = int(flv.get("disk") or 0) * GIB or int(image_virtual_size or 0)
        if size <= 0:
            raise OpenstackMappingError(f"{name}: cannot determine the root disk size (flavor disk 0, no image size)")
        rows.append({"id": f"{server['id']}:root", "device": root_dev, "size": size, "kind": "image_root"})
    for a in attachments:
        v = by_id.get(a["volume_id"])
        if v is None:
            raise OpenstackMappingError(f"{name}: attached volume {a['volume_id']} is not visible in Cinder "
                                        "(other project? use an admin/project-scoped account)")
        rows.append({"id": v["id"], "device": a.get("device"), "size": int(v["size"]) * GIB, "kind": "volume",
                     "multiattach": bool(v.get("multiattach"))})
    if not rows:
        raise OpenstackMappingError(f"{name}: server has no disks")
    rows.sort(key=lambda r: device_key(r["device"]))
    boot = next((r["id"] for r in rows if r["device"] == root_dev), rows[0]["id"])
    bus, ctrl = bus_and_controller(image_props)
    disks = [DiskSpec(id=r["id"], bus=bus, size_bytes=r["size"], order=i, is_boot=r["id"] == boot, bus_index=i,
                      label=(r["device"] or f"disk{i}").removeprefix("/dev/"), controller=ctrl,
                      source_handle={"kind": r["kind"], "device": r["device"], "volume_id": r["id"] if r["kind"] == "volume" else None},
                      shared_id=r["id"] if r.get("multiattach") else None)
             for i, r in enumerate(rows)]
    vif = str(image_props.get("hw_vif_model") or "virtio")
    nics = [NicSpec(id=n.get("port_id") or f"nic{i}", network=n.get("network") or "", mac=n.get("mac"), model=vif)
            for i, n in enumerate(interfaces)]
    spec = VmSpec(
        name=name, source_id=server["id"], cpu=CpuSpec(sockets=1, cores=max(1, int(flv.get("vcpus") or 1))),
        memory_bytes=int(flv.get("ram") or 512) << 20, disks=disks, nics=nics,
        firmware=Firmware.UEFI if str(image_props.get("hw_firmware_type")).lower() == "uefi" else Firmware.BIOS,
        guest_os=guest_os(image_props),
        extra={"openstack": {"image_backed": any(r["kind"] == "image_root" for r in rows),
                             "swap_mb": int(flv.get("swap") or 0),
                             "guest_agent": str(image_props.get("hw_qemu_guest_agent")).lower() in ("yes", "true", "1"),
                             "source_platform": "openstack"}})
    spec.validate()
    return spec


def bdm_snapshots(bdm: list[dict[str, Any]]) -> dict[str, str]:
    """Nova createImage of a volume-backed server records one Cinder snapshot per device in the
    image's block_device_mapping property: {device_name: snapshot_id}."""
    out = {}
    for b in bdm or []:
        if b.get("snapshot_id") and b.get("device_name"):
            out[b["device_name"]] = b["snapshot_id"]
    return out


def ceph_opts(cfg: Mapping[str, Any]) -> str:
    parts = [f"conf={cfg.get('ceph_conf', '/etc/ceph/ceph.conf')}", f"id={cfg.get('ceph_user', 'cinder')}"]
    if cfg.get("ceph_keyring"):
        parts.append(f"keyring={cfg['ceph_keyring']}")
    return ":".join(parts)


def rbd_volume_spec(cfg: Mapping[str, Any], volume_id: str, snapshot_id: str) -> str:
    return f"rbd:{cfg.get('cinder_pool', 'volumes')}/volume-{volume_id}@snapshot-{snapshot_id}:{ceph_opts(cfg)}"


def rbd_image_spec(cfg: Mapping[str, Any], location: str) -> str:
    """Glance image stored in RBD: location 'rbd://<fsid>/<pool>/<image>/<snap>'."""
    m = re.match(r"^rbd://[^/]+/([^/]+)/([^/]+)/([^/]+)$", location or "")
    if not m:
        raise OpenstackMappingError(f"Glance image is not stored in Ceph RBD ({location!r}); use transport 'attach'")
    pool, image, snap = m.groups()
    return f"rbd:{pool}/{image}@{snap}:{ceph_opts(cfg)}"
