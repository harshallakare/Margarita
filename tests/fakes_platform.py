"""Purpose: in-process fakes of Prism REST, OpenStack and qemu-img, so the Nutanix-source and
VHI-target adapters (and the full Nutanix -> VHI vertical) run in tests without any platform.

Usage: prism = FakePrism.with_vm(tmp_path, disks=13); api = FakeOpenStack(); qemu = FakeQemu()
"""

from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import Any

from vmigrate.core.errors import ProviderError
from vmigrate.providers.vhi.qemu import QCOW2_MAGIC

VM_UUID = "11111111-0000-0000-0000-000000000001"
CTR = "ctr-prod"


def qcow2_header(size: int) -> bytes:
    return QCOW2_MAGIC + struct.pack(">I", 3) + bytes(12) + struct.pack(">I", 20) + struct.pack(">Q", size) + bytes(72)


def v3_vm(name: str, disks: list[tuple[str, int, int]], *, cdrom=True, boot_index=0, boot_type="LEGACY") -> dict:
    dl = [{"uuid": f"dev-{u}", "disk_size_bytes": size,      # real Prism: v3 uuid = device, != vmdisk_uuid
           "device_properties": {"device_type": "DISK", "disk_address": {"adapter_type": "SCSI", "device_index": idx}},
           "storage_config": {"storage_container_reference": {"uuid": "ctr-uuid"}}}
          for (u, idx, size) in disks]
    if cdrom:
        dl.insert(1, {"uuid": "cd", "device_properties": {"device_type": "CDROM",
                                                          "disk_address": {"adapter_type": "IDE", "device_index": 0}}})
    return {
        "metadata": {"uuid": VM_UUID},
        "status": {"name": name, "cluster_reference": {"uuid": "cl"}, "resources": {
            "num_sockets": 2, "num_vcpus_per_socket": 4, "memory_size_mib": 16384, "power_state": "ON",
            "boot_config": {"boot_type": boot_type,
                            "boot_device": {"disk_address": {"adapter_type": "SCSI", "device_index": boot_index}}},
            "disk_list": dl,
            "nic_list": [{"uuid": "nic-1", "mac_address": "50:6b:8d:00:00:01", "model": "VIRTIO",
                          "subnet_reference": {"name": "VLAN-120"}, "ip_endpoint_list": [{"ip": "10.1.20.5"}]}],
            "guest_tools": {"nutanix_guest_tools": {"guest_os_version": "Linux:4.18.0-rhel"}},
        }},
    }


def v2_vm(name: str, uuid: str, disks: list[tuple[str, int, int]], *, power="on", cdrom=True) -> dict:
    info = [{"is_cdrom": False, "size": size, "storage_container_uuid": "ctr-uuid",
             "disk_address": {"device_bus": "scsi", "device_index": idx, "vmdisk_uuid": u, "device_uuid": f"dev-{u}",
                              "ndfs_filepath": f"/{CTR}/.acropolis/vmdisk/{u}"}}
            for (u, idx, size) in disks]
    if cdrom:
        info.append({"is_cdrom": True, "disk_address": {"device_bus": "ide", "device_index": 0}})
    return {"uuid": uuid, "name": name, "power_state": power, "vm_disk_info": info}


class FakePrism:
    """Implements the PrismClient surface used by NutanixSource."""

    def __init__(self, vm_name: str, disks: list[tuple[str, int, int]], nfs_root: Path, extra_vms: int = 0):
        self.vm_name = vm_name
        self.disks = disks
        self.nfs_root = nfs_root
        self.v2_disks_override: list | None = None
        self.pd_vms = [VM_UUID]
        self.snapshots: list[dict] = []
        self.clone: dict | None = None
        self.deleted: list[str] = []
        self.posts: list[tuple[str, Any]] = []
        self.extra_vms = extra_vms

    @classmethod
    def with_vm(cls, tmp: Path, disks: int = 13, size: int = 3 << 20, name: str = "RIS-Sainath", **kw) -> FakePrism:
        d = [(f"vd-{i:02d}", i, size) for i in range(disks)]
        nfs = tmp / "nfs" / CTR / ".acropolis" / "vmdisk"
        nfs.mkdir(parents=True)
        return cls(name, d, tmp / "nfs", **kw)

    # --- v3
    def v3_post(self, path: str, body: Any) -> Any:
        if path == "/clusters/list":
            return {"entities": [{"metadata": {"uuid": "cl"}}], "metadata": {"total_matches": 1}}
        assert path == "/vms/list"
        all_vms = [{"metadata": {"uuid": VM_UUID}, "status": {"name": self.vm_name}}] + [
            {"metadata": {"uuid": f"other-{i}"}, "status": {"name": f"other-{i}"}} for i in range(self.extra_vms)]
        off, n = body["offset"], body["length"]
        return {"entities": all_vms[off:off + n], "metadata": {"total_matches": len(all_vms)}}

    def v3_get(self, path: str) -> Any:
        assert path == f"/vms/{VM_UUID}"
        return v3_vm(self.vm_name, self.disks)

    # --- v1 / v2
    def v1_post(self, path: str, body: Any) -> Any:
        self.posts.append((path, body))
        assert path.endswith("/oob_schedules")
        import time
        self.snapshots.append({"snapshot_id": 4242, "snapshot_create_time_usecs": int(time.time() * 1e6),
                               "vms": [{"vm_id": VM_UUID}], "state": "AVAILABLE"})
        return {}

    def v2_get(self, path: str, params: dict | None = None) -> Any:
        if path == "/cluster":
            return {"name": "lab-cluster", "uuid": "cl"}
        if path == f"/vms/{VM_UUID}":
            disks = self.v2_disks_override if self.v2_disks_override is not None else self.disks
            return v2_vm(self.vm_name, VM_UUID, disks)
        if self.clone and path == f"/vms/{self.clone['uuid']}":
            return self.clone
        if path == "/vms/":
            name = params["filter"].split("==", 1)[1]
            return {"entities": [self.clone] if self.clone and self.clone["name"] == name else []}
        if path.startswith("/protection_domains/") and path.endswith("/dr_snapshots"):
            return {"entities": self.snapshots}
        if path.startswith("/protection_domains/"):
            return {"vms": [{"vm_id": v} for v in self.pd_vms]}
        raise ProviderError(f"FakePrism: unexpected GET {path}")

    def v2_post(self, path: str, body: Any) -> Any:
        self.posts.append((path, body))
        if path.endswith("/restore_entities"):
            cname = body["vm_name_prefix"] + self.vm_name
            cdisks = [(f"clone-{u}", idx, size) for (u, idx, size) in self.disks]
            from vmigrate.providers.fake.provider import PatternDisk
            for (cu, idx, size) in cdisks:
                (self.nfs_root / CTR / ".acropolis" / "vmdisk" / cu).write_bytes(PatternDisk(f"ntnx:{idx}", size).read(0, size))
            self.clone = v2_vm(cname, "clone-vm-uuid", cdisks, power="off", cdrom=False)
            return {"value": True}
        if path.endswith("/protect_vms"):
            self.pd_vms += body["uuids"]
            return {}
        raise ProviderError(f"FakePrism: unexpected POST {path}")

    def v2_delete(self, path: str, params: dict | None = None) -> Any:
        self.deleted.append(path)
        return {}


class FakeOpenStack:
    """Implements the OpenStackApi surface used by VhiTarget, backed by a fake vStorage dir."""

    def __init__(self, vstorage: Path, project_id: str = "proj-1"):
        self.vstorage = vstorage
        self.project_id = project_id
        self.volumes: dict[str, dict] = {}
        self.servers: dict[str, dict] = {}
        self.actions: list[tuple[str, dict]] = []
        self.console = "CentOS Linux 8\nris-sainath login: "
        self.ports: dict[str, dict] = {}
        self._n = 0

    def _id(self, p: str) -> str:
        self._n += 1
        return f"{p}-{self._n:04d}"

    def current_project_id(self): return self.project_id
    def find_volume_type(self, name): return {"name": name} if name == "vmds" else None

    def create_volume(self, name, size_gib, volume_type, metadata, availability_zone=None):
        vid = self._id("vol")
        d = self.vstorage / "vols/datastores/cinder" / f"volume-{vid}"
        d.mkdir(parents=True)
        (d / f"volume-{vid}").write_bytes(qcow2_header(size_gib << 30))
        (d / ".info").write_text("sidecar")
        v = {"id": vid, "name": name, "size": size_gib, "status": "available", "metadata": dict(metadata), "attachments": []}
        v["volume_type"] = volume_type or "__default__"
        self.volumes[vid] = v
        return dict(v)

    def get_volume(self, vid): return dict(self.volumes[vid])
    def volumes_by_name(self, name): return [dict(v) for v in self.volumes.values() if v["name"] == name]
    def delete_volume(self, vid): self.volumes.pop(vid, None)
    def volume_action(self, vid, body): self.actions.append((vid, body))
    def flavors(self): return [{"id": "f-big", "vcpus": 16, "ram": 65536}, {"id": "f-8x16", "vcpus": 8, "ram": 16384}]
    def find_flavor(self, n): return next((f for f in self.flavors() if f["id"] == n), None)
    def networks(self): return [{"id": "n-2", "name": "prod-net", "is_shared": True}, {"id": "n-1", "name": "backup"}]
    def volume_types(self): return [{"name": "vmds"}, {"name": "vmds-hdd"}]

    def create_server(self, **kw):
        sid = self._id("srv")
        atts = []
        for i, b in enumerate(kw["block_device_mapping"]):
            atts.append({"volume_id": b["uuid"], "device": f"/dev/sd{chr(ord('a') + i)}"})
            self.volumes[b["uuid"]]["status"] = "in-use"
            self.volumes[b["uuid"]]["attachments"] = [{"server_id": sid}]
        self.servers[sid] = {"id": sid, "name": kw["name"], "status": "ACTIVE", "kw": kw, "atts": atts,
                             "metadata": kw.get("metadata", {})}
        return {"id": sid, "status": "BUILD"}

    def get_server(self, sid): return dict(self.servers[sid])
    def find_server(self, name): return next((dict(s) for s in self.servers.values() if s["name"] == name), None)
    def delete_server(self, sid): self.servers.pop(sid, None)

    def attach_volume(self, sid, vid):
        s = self.servers[sid]
        n = len(s["atts"])
        dev = "/dev/sd" + ("a" + chr(ord("a") + n - 26) if n >= 26 else chr(ord("a") + n))
        s["atts"].append({"volume_id": vid, "device": dev})
        self.volumes[vid]["status"] = "in-use"

    def attachments(self, sid): return list(self.servers[sid]["atts"])
    def console_log(self, sid, length=300): return self.console
    def find_network(self, n): return {"id": f"net-{n}"} if n.startswith("uuid-") else None
    def create_port(self, **kw):
        pid = self._id("port"); self.ports[pid] = kw; return {"id": pid}
    def ports_by_name(self, name): return [{"id": k} for k, v in self.ports.items() if v["name"] == name]
    def delete_port(self, pid): self.ports.pop(pid, None)


class FakeQemu:
    """qemu-img stand-in: `convert` writes a qcow2 header into the backing and remembers the raw
    content so `read_range` (qemu-img dd) can serve verification reads."""

    binary = "true"  # any binary on PATH, so preflight's which() check passes

    def __init__(self, virtual_size_delta: int = 0):
        self.calls: list[list[str]] = []
        self.content: dict[str, str] = {}
        self.delta = virtual_size_delta

    def run(self, argv, timeout=None):
        self.calls.append(argv)
        if argv[1] == "convert":
            src, dst = argv[-2], argv[-1]
            keep = Path(dst + ".fakedata")  # hard link keeps the (sparse) bytes after staging is freed
            keep.unlink(missing_ok=True)
            os.link(src, keep)
            self.content[dst] = str(keep)
            Path(dst).write_bytes(qcow2_header(os.path.getsize(src) + self.delta))
        return ""

    def read_range(self, image, fmt, offset, length):
        with open(self.content[str(image)], "rb") as f:
            return os.pread(f.fileno(), length, offset)
