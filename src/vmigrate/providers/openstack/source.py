"""Purpose: OpenStack SourceProvider (Nova servers with Cinder volumes and/or an image-backed root disk).

Usage (endpoint config; password + SSH key go in the encrypted credentials):
    type: openstack
    auth_url: https://keystone.example.com:5000/v3
    username: migrate
    password: ...
    project_id: 0123abcd...              # scope by ID (pitfall 7); user_domain_name: Default
    region_name: RegionOne
    transport: rbd                       # rbd (Ceph-backed clouds) | attach (worker is an instance in this cloud)
    # rbd transport
    ceph_access: local                   # local (this worker has ceph.conf + keyring) | ssh (via a Ceph host)
    ceph_conf: /etc/ceph/ceph.conf
    ceph_user: cinder
    ceph_keyring: /etc/ceph/ceph.client.cinder.keyring
    cinder_pool: volumes
    ceph_host: ceph-mon1                 # ssh access: host, ssh_user, ssh_private_key, ssh_known_hosts (pinned)
    # attach transport
    worker_server_id: <uuid>             # default: from the metadata service

How it works:
  inspect  : Nova server + flavor, os-volume_attachments, Cinder volumes, image or boot-volume
             metadata (hw_disk_bus/hw_scsi_model -> controller), interfaces -> IR
  gate     : Nova os-volume_attachments count vs Cinder volumes attached to the server (+ the
             image-backed root), plus the snapshot's disk count
  refuse   : flavor ephemeral disks (never in snapshots), volumes not visible in Cinder,
             app_consistent without hw_qemu_guest_agent, app_consistent across an image-backed
             root + volumes (Nova cannot freeze both at once), non-unique server names
  snapshot : Nova createImage "vmigrate-<run>": volume-backed -> one Cinder snapshot per volume
             (quiesced together with the guest agent); image-backed root -> Glance image, plus a
             forced Cinder snapshot per attached volume (crash-consistent per disk)
  read     : rbd    -> qemu-nbd --read-only rbd:<pool>/volume-<v>@snapshot-<s> (or the Glance image's
                       RBD snapshot), locally or on a Ceph host over pinned SSH
             attach -> temporary volume from the snapshot/image, attached to this worker, read as a
                       block device (O_RDONLY), then detached and deleted
  CBT      : rbd transport + volume disks only: rbd diff --from-snap snapshot-<prev>; the previous
             snapshot set is kept as the diff base. Otherwise replicas use compare mode.
  cleanup  : the vmigrate-* image, its Nova-created snapshots, our forced snapshots, temp volumes.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Mapping
from typing import Any

from ...core.context import RunContext
from ...core.errors import ChangeTrackingUnavailable, ConfigError, LiveDiskReadRefused, ProviderError, SafetyGateError
from ...core.fileio import RawFileReader
from ...core.interfaces import CheckpointRef, DiskReader, SnapshotRef, SourceProvider, VmRef
from ...core.ranges import ByteRange, normalize
from ...ir import DiskSpec, VmSpec
from ..common.openstack_auth import project_problem
from . import mapping
from .cloud import PREFIX

_SAFE = re.compile(r"^[A-Za-z0-9/][A-Za-z0-9_.+/:@=-]*$")


def block_device_size(path: str) -> int:
    fd = os.open(path, os.O_RDONLY)
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)


class AttachedVolumeReader(DiskReader):
    """Temporary volume from a snapshot/image, attached to this worker, read as a block device."""

    def __init__(self, cloud: Any, worker_id: str, *, name: str, size_bytes: int, snapshot_id: str | None = None,
                 image_id: str | None = None, dev_timeout: float = 180, by_id_dir: str = "/dev/disk/by-id"):
        self.cloud, self.worker = cloud, worker_id
        self.vol: str | None = None
        self.attached = False
        self._r: RawFileReader | None = None
        try:
            self.vol = cloud.volume_from(name=name, size_gib=max(1, math.ceil(size_bytes / mapping.GIB)),
                                         snapshot_id=snapshot_id, image_id=image_id)
            cloud.attach(worker_id, self.vol)
            self.attached = True
            short = self.vol[:20]
            cands = [f"{by_id_dir}/virtio-{short}", f"{by_id_dir}/scsi-0QEMU_QEMU_HARDDISK_{short}",
                     f"{by_id_dir}/scsi-SQEMU_QEMU_HARDDISK_{short}"]
            deadline = time.monotonic() + dev_timeout
            dev = None
            while dev is None:
                dev = next((c for c in cands if os.path.exists(c)), None)
                if dev is None:
                    if time.monotonic() > deadline:
                        raise ProviderError(f"volume {self.vol} attached but no device appeared under {by_id_dir}")
                    time.sleep(1)
            self._r = RawFileReader(dev, size=block_device_size(dev))
        except Exception:
            self.close()
            raise

    @property
    def size(self) -> int:
        assert self._r is not None
        return self._r.size

    def read(self, offset: int, length: int) -> bytes:
        assert self._r is not None
        return self._r.read(offset, length)

    def close(self) -> None:
        if self._r is not None:
            self._r.close()
            self._r = None
        if self.vol:
            if self.attached:
                self.cloud.detach(self.worker, self.vol)
                self.attached = False
            self.cloud.delete_volume(self.vol)
            self.vol = None


class OpenstackSource(SourceProvider):
    type_name = "openstack"

    def __init__(self, name: str, config: Mapping[str, Any], ctx: RunContext, cloud: Any = None,
                 shell_factory: Any = None, export_factory: Any = None, attach_factory: Any = None):
        super().__init__(name, config, ctx)
        self.transport = config.get("transport", "rbd")
        if self.transport not in ("rbd", "attach"):
            raise ConfigError("openstack transport must be rbd or attach")
        self._cloud = cloud
        self._shell_factory, self._export_factory, self._attach_factory = shell_factory, export_factory, attach_factory
        self._shell: Any = None
        self._vm: dict[str, dict[str, Any]] = {}

    @property
    def cloud(self) -> Any:
        if self._cloud is None:
            from .cloud import OsCloud
            self._cloud = OsCloud(self.config)
        return self._cloud

    def shell(self) -> Any:
        if self._shell is None:
            if self._shell_factory is not None:
                self._shell = self._shell_factory()
            elif self.config.get("ceph_access", "local") == "ssh":
                from ..common.remote import SshNode
                self._shell = SshNode(self.config["ceph_host"], self.config.get("ssh_user", "root"),
                                      self.config.get("ssh_private_key", ""), self.config.get("ssh_known_hosts", ""),
                                      port=int(self.config.get("ssh_port", 22)))
            else:
                from ..common.remote import LocalShell
                self._shell = LocalShell()
        return self._shell

    def close(self) -> None:
        if self._shell is not None and hasattr(self._shell, "close"):
            self._shell.close()
        self._shell = None

    # ------------------------------------------------------------------ inventory
    def _load(self, vm: VmRef) -> dict[str, Any]:
        c = self.cloud
        srv = c.server(vm.id)
        atts = c.nova_attachments(vm.id)
        vols = c.volumes_for(vm.id)
        props: dict[str, Any] = {}
        vsize = None
        if mapping.image_backed(srv, atts):
            img = c.image(srv["image_id"])
            props, vsize = img["props"], img.get("virtual_size")
        else:
            root = srv.get("root_device_name") or "/dev/vda"
            boot = next((a["volume_id"] for a in atts if a.get("device") == root), None)
            props = next((v["image_meta"] for v in vols if v["id"] == boot), {}) or {}
        d = {"server": srv, "atts": atts, "vols": vols, "props": props, "vsize": vsize,
             "ifaces": c.interfaces(vm.id)}
        self._vm[vm.id] = d
        return d

    def check_connection(self) -> list[str]:
        try:
            pid = self.cloud.project_id()
        except Exception as e:  # noqa: BLE001
            return [f"cannot authenticate to OpenStack: {e}"]
        return project_problem(pid, self.config)

    def preflight(self, spec: VmSpec | None = None) -> list[str]:
        try:
            self.cloud.project_id()
        except Exception as e:  # noqa: BLE001
            return [f"cannot authenticate to OpenStack: {e}"]
        p: list[str] = []
        if self.transport == "rbd" and self.config.get("ceph_access", "local") == "ssh":
            for k in ("ceph_host", "ssh_private_key", "ssh_known_hosts"):
                if not self.config.get(k):
                    p.append(f"{k} is required for ceph_access: ssh")
        return p

    def list_vms(self) -> list[VmRef]:
        return [VmRef(s["id"], s["name"], self.name) for s in self.cloud.servers()]

    def find_vm(self, name_or_id: str) -> VmRef:
        srv = self.cloud.servers()
        hit = [s for s in srv if s["id"] == name_or_id] or [s for s in srv if s["name"] == name_or_id]
        if not hit:
            raise ProviderError(f"openstack: no server {name_or_id!r}")
        if len(hit) > 1:
            raise SafetyGateError(f"openstack: {len(hit)} servers are named {name_or_id!r}; use the id "
                                  f"({', '.join(s['id'] for s in hit)})")
        return VmRef(hit[0]["id"], hit[0]["name"], self.name)

    def inspect_vm(self, vm: VmRef) -> VmSpec:
        d = self._load(vm)
        try:
            return mapping.to_vmspec(d["server"], d["atts"], d["vols"], d["props"], d["ifaces"], d["vsize"])
        except mapping.OpenstackMappingError as e:
            raise SafetyGateError(str(e)) from e

    def disk_count_crosscheck(self, vm: VmRef) -> dict[str, int]:
        d = self._vm.get(vm.id) or self._load(vm)
        return mapping.crosscheck(d["server"], d["atts"], d["vols"])

    # ------------------------------------------------------------------ snapshots
    def shutdown_vm(self, vm: VmRef, *, force: bool = False, timeout: int = 900) -> str:
        self.ctx.require_execute("nova stop", server=vm.name)
        return f"{vm.name} {self.cloud.stop_server(vm.id)}"

    def create_snapshot(self, vm: VmRef, app_consistent: bool) -> SnapshotRef:
        d = self._vm.get(vm.id) or self._load(vm)
        spec = mapping.to_vmspec(d["server"], d["atts"], d["vols"], d["props"], d["ifaces"], d["vsize"])
        ext = spec.extra["openstack"]
        if app_consistent and not ext["guest_agent"]:
            raise SafetyGateError(f"{spec.name}: app-consistent snapshot requested but the image/boot volume lacks "
                                  "hw_qemu_guest_agent=yes, so Nova cannot quiesce. Set it (and run the agent), or "
                                  "use app_consistent: false.")
        vols = [x for x in spec.disks if x.source_handle["kind"] == "volume"]
        if ext["image_backed"] and vols and app_consistent:
            raise SafetyGateError(f"{spec.name}: image-backed root + {len(vols)} volume(s) cannot be snapshotted "
                                  "consistently together (Nova only quiesces volume-backed servers as a set). Use "
                                  "app_consistent: false (crash-consistent per disk) or stop the server.")
        name = f"{PREFIX}{self.ctx.run_id}"
        self.ctx.require_execute("nova createImage", server=spec.name, name=name)
        img = self.cloud.create_server_image(vm.id, name)
        data: dict[str, Any] = {"server_id": vm.id, "name": name, "image_id": img["id"], "disks": {}, "own_snaps": []}
        ref = SnapshotRef(id=img["id"], vm=vm, app_consistent=app_consistent, data=data)
        try:
            bdm = mapping.bdm_snapshots(img.get("bdm") or [])
            for x in spec.disks:
                if x.source_handle["kind"] == "image_root":
                    loc = img.get("direct_url") or next((loc.get("url") for loc in img.get("locations") or []), None)
                    data["disks"][x.id] = {"kind": "image", "image_id": img["id"], "location": loc,
                                           "disk_format": img.get("disk_format")}
                elif not ext["image_backed"]:
                    sid = bdm.get(x.source_handle["device"])
                    if not sid:
                        raise SafetyGateError(f"Nova image {img['id']} has no snapshot for {x.source_handle['device']}")
                    data["disks"][x.id] = {"kind": "volume", "volume_id": x.id, "snapshot_id": sid}
                else:   # volume attached to an image-backed server: Nova does not snapshot it
                    self.ctx.logger.warning("%s: snapshotting %s separately (crash-consistent, not at the same "
                                            "instant as the root disk)", spec.name, x.label)
                    sid = self.cloud.create_volume_snapshot(x.id, f"{name}-{x.label}")
                    data["own_snaps"].append(sid)
                    data["disks"][x.id] = {"kind": "volume", "volume_id": x.id, "snapshot_id": sid}
        except Exception:
            self.cleanup(ref)
            raise
        return ref

    def snapshot_disk_count(self, snapshot: SnapshotRef) -> int | None:
        return len(snapshot.data.get("disks") or {})

    def cleanup(self, snapshot: SnapshotRef) -> None:
        self.ctx.require_execute("delete vmigrate snapshot set", image=snapshot.data.get("image_id"))
        name = snapshot.data.get("name")
        for sid in snapshot.data.get("own_snaps") or []:
            self.cloud.delete_snapshot(sid)
        for d in (snapshot.data.get("disks") or {}).values():
            if d.get("kind") == "volume" and d["snapshot_id"] not in (snapshot.data.get("own_snaps") or []):
                self.cloud.delete_snapshot(d["snapshot_id"], created_by_image=name)
        if snapshot.data.get("image_id"):
            self.cloud.delete_image(snapshot.data["image_id"])

    # ------------------------------------------------------------------ reads
    def _rbd_target(self, sd: dict[str, Any]) -> str:
        if sd["kind"] == "volume":
            t = mapping.rbd_volume_spec(self.config, sd["volume_id"], sd["snapshot_id"])
        else:
            if (sd.get("disk_format") or "raw") != "raw":
                raise SafetyGateError(f"Glance image {sd['image_id']} is {sd.get('disk_format')}; the rbd transport "
                                      "reads raw images only (use transport attach)")
            t = mapping.rbd_image_spec(self.config, sd.get("location") or "")
        if not _SAFE.match(t):
            raise SafetyGateError(f"refusing unsafe rbd target {t!r}")
        return t

    def open_disk(self, snapshot: SnapshotRef, disk: DiskSpec) -> DiskReader:
        if not snapshot.data.get("name", "").startswith(PREFIX):
            raise LiveDiskReadRefused("openstack: reads require a vmigrate snapshot")
        sd = (snapshot.data.get("disks") or {}).get(disk.id)
        if sd is None:
            raise SafetyGateError(f"disk {disk.label} missing from snapshot {snapshot.id}")
        tag = f"{PREFIX}{self.ctx.run_id}-{disk.order:02d}"
        if self.transport == "rbd":
            try:
                plan = {"setup": [], "nbd_target": self._rbd_target(sd), "nbd_format": "raw", "nbd_extra": [],
                        "teardown": []}
            except mapping.OpenstackMappingError as e:
                raise SafetyGateError(str(e)) from e
            if self._export_factory is not None:
                r = self._export_factory(self.shell(), plan, tag, snapshot, disk)
            else:
                from ..common.remote import NodeExport
                r = NodeExport(self.shell(), plan, tag=tag)
        else:
            worker = self.config.get("worker_server_id")
            if not worker:
                from .cloud import worker_server_id
                worker = worker_server_id()
            kw = {"snapshot_id": sd["snapshot_id"]} if sd["kind"] == "volume" else {"image_id": sd["image_id"]}
            factory = self._attach_factory or AttachedVolumeReader
            r = factory(self.cloud, worker, name=tag, size_bytes=disk.size_bytes, **kw)
        if r.size < disk.size_bytes:
            r.close()
            raise SafetyGateError(f"{disk.label}: readable size {r.size} < inspected {disk.size_bytes}")
        return r

    # ------------------------------------------------------------------ CBT (rbd diff on Cinder volumes)
    def supports_cbt(self) -> bool:
        if self.transport != "rbd":
            return False
        return any(not mapping.image_backed(v["server"], v["atts"]) for v in self._vm.values())

    def keep_for_cbt(self) -> bool:
        return self.supports_cbt()

    def checkpoint(self, snapshot: SnapshotRef) -> CheckpointRef:
        snaps = {k: v.get("snapshot_id") for k, v in (snapshot.data.get("disks") or {}).items()}
        return CheckpointRef(id=snapshot.id, data={"snaps": snaps})

    def changed_regions(self, disk: DiskSpec, base: CheckpointRef, cur: CheckpointRef) -> list[ByteRange]:
        b = (base.data.get("snaps") or {}).get(disk.id)
        c = (cur.data.get("snaps") or {}).get(disk.id)
        if not b or not c or disk.source_handle.get("kind") != "volume":
            raise ChangeTrackingUnavailable(f"{disk.label}: no snapshot pair for rbd diff")
        pool = self.config.get("cinder_pool", "volumes")
        argv = ["rbd", "diff", "--format", "json", "--from-snap", f"snapshot-{b}",
                f"{pool}/volume-{disk.id}@snapshot-{c}", "--conf", self.config.get("ceph_conf", "/etc/ceph/ceph.conf"),
                "--id", self.config.get("ceph_user", "cinder")]
        if self.config.get("ceph_keyring"):
            argv += ["--keyring", self.config["ceph_keyring"]]
        if not all(_SAFE.match(a) or a.startswith("--") for a in argv):
            raise ChangeTrackingUnavailable("unsafe rbd diff arguments")
        try:
            entries = json.loads(self.shell().run(argv) or "[]")
        except (ProviderError, ValueError) as e:
            raise ChangeTrackingUnavailable(f"rbd diff for {disk.label}: {e}") from e
        return normalize(ByteRange(int(x["offset"]), int(x["length"])) for x in entries)
