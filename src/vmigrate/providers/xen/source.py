"""Purpose: XenServer / Citrix Hypervisor / XCP-ng SourceProvider over XAPI JSON-RPC.

Usage (endpoint config; the password goes in the encrypted credentials):
    type: xen
    url: https://xen1.example.com          # pool master (a member redirects to it)
    username: root
    password: ...                          # credential
    ca_pem: "-----BEGIN CERTIFICATE-----..."   # the host certificate / CA (or verify_tls: false + know why)
    verify_tls: true
    transport: auto                        # auto | nbd | export
    enable_cbt: false                      # opt-in: turn on changed block tracking on the VM's disks

How it works:
  inspect  : VM, VBDs, VDIs, VIFs, networks and guest metrics -> IR (xen-blkfront / xen-netfront:
             OSMorph injects virtio for KVM targets)
  gate     : VM.VBDs vs the pool's VBD table filtered by this VM
  refuse   : app_consistent (quiesced snapshots were removed in XenServer 8 / XCP-ng 8)
  snapshot : VM.snapshot named vmigrate-<run> (crash-consistent, all disks at once)
  read     : NBD (XAPI VDI.get_nbd_info: hosts on a network with purpose "nbd" serve TLS with the
             host certificate, which is pinned from the API; "insecure_nbd" serves plain NBD). Without
             an NBD network (transport auto) the disk is streamed from export_raw_vdi instead; that
             stream is sequential, so a backwards read restarts it.
  CBT      : disks with CBT enabled: VDI.list_changed_blocks(previous snapshot VDI, this one), a
             bitmap of 64 KiB blocks. After a sync the snapshot's data is dropped (VDI.data_destroy)
             and only its CBT metadata is kept as the next base, so a replica costs no snapshot space.
             enable_cbt: true turns CBT on for disks that lack it (a metadata change on the source).
  cleanup  : VDI.destroy + VM.destroy, only for snapshots named vmigrate-*
Tested against a fake XAPI only; NOT run against a real pool yet.
"""

from __future__ import annotations

import http.client
import ssl
import urllib.parse
from collections.abc import Mapping
from typing import Any

from ...core.context import RunContext
from ...core.errors import ChangeTrackingUnavailable, ConfigError, LiveDiskReadRefused, ProviderError, SafetyGateError
from ...core.interfaces import CheckpointRef, DiskReader, SnapshotRef, SourceProvider, VmRef
from ...core.nbd import NbdReader
from ...core.ranges import ByteRange
from ...ir import DiskSpec, VmSpec
from . import mapping
from .api import XapiError

PREFIX = "vmigrate-"


class ExportStream(DiskReader):
    """export_raw_vdi as a DiskReader: sequential stream; a read behind the stream position
    restarts it (verify samples do that; the copy itself is sequential)."""

    def __init__(self, url_fn: Any, size: int, context: ssl.SSLContext | None, opener: Any = None):
        self.url_fn, self._size, self.context = url_fn, size, context
        self._opener = opener or self._open_https
        self._resp: Any = None
        self._pos = 0
        self.restarts = 0

    def _open_https(self, url: str) -> Any:
        u = urllib.parse.urlsplit(url)
        c = http.client.HTTPSConnection(u.netloc, timeout=600, context=self.context)
        c.request("GET", u.path + "?" + u.query)
        r = c.getresponse()
        if r.status != 200:
            raise ProviderError(f"export_raw_vdi: HTTP {r.status} {r.read(200)!r}")
        return r

    @property
    def size(self) -> int:
        return self._size

    def _skip(self, n: int) -> None:
        while n > 0:
            b = self._resp.read(min(n, 8 << 20))
            if not b:
                raise ProviderError("export_raw_vdi stream ended early")
            n -= len(b)

    def read(self, offset: int, length: int) -> bytes:
        length = max(0, min(length, self._size - offset))
        if length == 0:
            return b""
        if self._resp is None or offset < self._pos:
            if self._resp is not None:
                self._resp.close()
                self.restarts += 1
            self._resp, self._pos = self._opener(self.url_fn()), 0
        self._skip(offset - self._pos)
        out = bytearray()
        while len(out) < length:
            b = self._resp.read(length - len(out))
            if not b:
                raise ProviderError("export_raw_vdi stream ended early")
            out += b
        self._pos = offset + length
        return bytes(out)

    def close(self) -> None:
        if self._resp is not None:
            self._resp.close()
            self._resp = None


class XenSource(SourceProvider):
    type_name = "xen"

    def __init__(self, name: str, config: Mapping[str, Any], ctx: RunContext, api: Any = None,
                 nbd_connect: Any = None, export_opener: Any = None):
        super().__init__(name, config, ctx)
        for k in ("url", "username"):
            if not config.get(k):
                raise ConfigError(f"xen source {name}: missing {k!r}")
        if (config.get("transport") or "auto") not in ("auto", "nbd", "export"):
            raise ConfigError("xen: transport must be auto, nbd or export")
        self._api = api
        self._nbd_connect = nbd_connect
        self._export_opener = export_opener
        self._vm: dict[str, dict[str, Any]] = {}

    @property
    def api(self) -> Any:
        if self._api is None:
            from .api import XenApi
            c = self.config
            self._api = XenApi(c["url"], c["username"], c.get("password", ""), ca_pem=c.get("ca_pem"),
                               verify_tls=bool(c.get("verify_tls", True)))
        return self._api

    def close(self) -> None:
        if self._api is not None and hasattr(self._api, "logout"):
            self._api.logout()

    # ------------------------------------------------------------------ inventory
    def _vms(self) -> dict[str, dict[str, Any]]:
        return {ref: v for ref, v in self.api.call("VM.get_all_records").items()
                if not v.get("is_a_template") and not v.get("is_control_domain") and not v.get("is_a_snapshot")}

    def _ref(self, uuid: str) -> str:
        return self.api.call("VM.get_by_uuid", uuid)

    def _load(self, vm: VmRef) -> dict[str, Any]:
        ref = self._ref(vm.id)
        rec = self.api.call("VM.get_record", ref)
        vbds = {b: self.api.call("VBD.get_record", b) for b in rec.get("VBDs") or []}
        vdis = {v["VDI"]: self.api.call("VDI.get_record", v["VDI"]) for v in vbds.values()
                if v.get("type") == "Disk" and v.get("VDI") not in (None, "", "OpaqueRef:NULL")}
        vifs = [self.api.call("VIF.get_record", f) for f in rec.get("VIFs") or []]
        nets = {f["network"]: self.api.call("network.get_name_label", f["network"]) for f in vifs if f.get("network")}
        gm = rec.get("guest_metrics")
        metrics = self.api.call("VM_guest_metrics.get_record", gm) if gm and gm != "OpaqueRef:NULL" else None
        d = {"ref": ref, "rec": rec, "vbds": vbds, "vdis": vdis, "vifs": vifs, "nets": nets, "metrics": metrics}
        self._vm[vm.id] = d
        return d

    def _spec(self, d: dict[str, Any]) -> VmSpec:
        try:
            return mapping.to_vmspec(d["rec"], d["vbds"], d["vdis"], d["vifs"], d["nets"], d["metrics"])
        except mapping.XenMappingError as e:
            raise SafetyGateError(str(e)) from e

    def check_connection(self) -> list[str]:
        try:
            self.api.login()
            self._vms()
        except Exception as e:  # noqa: BLE001
            return [f"cannot use XAPI at {self.config['url']} as {self.config['username']}: {e}"]
        return []

    def preflight(self, spec: VmSpec | None = None) -> list[str]:
        try:
            self.api.login()
        except Exception as e:  # noqa: BLE001
            return [f"cannot log in to XAPI at {self.config['url']}: {e}"]
        return []

    def list_vms(self) -> list[VmRef]:
        return [VmRef(v["uuid"], v.get("name_label") or v["uuid"], self.name) for v in self._vms().values()]

    def find_vm(self, name_or_id: str) -> VmRef:
        vms = list(self._vms().values())
        hit = [v for v in vms if v.get("uuid") == name_or_id] or [v for v in vms if v.get("name_label") == name_or_id]
        if not hit:
            raise ProviderError(f"xen: no VM {name_or_id!r}")
        if len(hit) > 1:
            raise SafetyGateError(f"xen: {len(hit)} VMs are named {name_or_id!r}; use the UUID "
                                  f"({', '.join(v['uuid'] for v in hit)})")
        return VmRef(hit[0]["uuid"], hit[0].get("name_label") or hit[0]["uuid"], self.name)

    def inspect_vm(self, vm: VmRef) -> VmSpec:
        return self._spec(self._load(vm))

    def disk_count_crosscheck(self, vm: VmRef) -> dict[str, int]:
        d = self._vm.get(vm.id) or self._load(vm)
        pool = self.api.call("VBD.get_all_records")
        n = sum(1 for b in pool.values() if b.get("VM") == d["ref"] and b.get("type") == "Disk")
        return mapping.crosscheck(d["vbds"], n)

    def shutdown_vm(self, vm: VmRef, *, force: bool = False, timeout: int = 900) -> str:
        ref = self._ref(vm.id)
        if self.api.call("VM.get_power_state", ref) == "Halted":
            return f"{vm.name} already halted"
        self.ctx.require_execute("xen hard_shutdown" if force else "xen clean_shutdown", vm=vm.id)
        self.api.call("VM.hard_shutdown" if force else "VM.clean_shutdown", ref)
        return f"{vm.name} {'stopped (hard)' if force else 'shut down'}"

    # ------------------------------------------------------------------ snapshots
    def create_snapshot(self, vm: VmRef, app_consistent: bool) -> SnapshotRef:
        if app_consistent:
            raise SafetyGateError("xen: quiesced snapshots were removed in XenServer 8 / XCP-ng 8; snapshots are "
                                  "crash-consistent. Set app_consistent: false, or shut the VM down first.")
        d = self._load(vm)
        spec = self._spec(d)
        if self.config.get("enable_cbt"):
            for disk in spec.disks:
                if not disk.source_handle.get("cbt_enabled"):
                    self.ctx.require_execute("xen VDI.enable_cbt", vdi=disk.id)
                    self.api.call("VDI.enable_cbt", disk.source_handle["vdi_ref"])
                    disk.source_handle["cbt_enabled"] = True
        name = f"{PREFIX}{self.ctx.run_id}"
        self.ctx.require_execute("xen VM.snapshot", vm=vm.id, name=name)
        snap_ref = self.api.call("VM.snapshot", d["ref"], name)
        ref = SnapshotRef(id=name, vm=vm, app_consistent=False,
                          data={"snapshot_ref": snap_ref, "vdis": {}, "cbt": {}})
        try:
            by_src = {}
            for b in self.api.call("VM.get_VBDs", snap_ref):
                vbd = self.api.call("VBD.get_record", b)
                if vbd.get("type") != "Disk" or vbd.get("VDI") in (None, "", "OpaqueRef:NULL"):
                    continue
                svdi = self.api.call("VDI.get_record", vbd["VDI"])
                src = svdi.get("snapshot_of")
                src_uuid = d["vdis"][src]["uuid"] if src in d["vdis"] else None
                if src_uuid:
                    by_src[src_uuid] = {"ref": vbd["VDI"], "uuid": svdi["uuid"]}
            want = {x.id for x in spec.disks}
            if set(by_src) != want:
                raise SafetyGateError(f"snapshot covers {sorted(by_src)} but the VM was inspected with {sorted(want)}")
            ref.data["vdis"].update(by_src)
            ref.data["cbt"].update({x.id: bool(x.source_handle.get("cbt_enabled")) for x in spec.disks})
        except Exception:
            self.cleanup(ref)
            raise
        return ref

    def snapshot_disk_count(self, snapshot: SnapshotRef) -> int | None:
        return len(snapshot.data.get("vdis") or {})

    def _nbd(self, vdi_ref: str) -> DiskReader | None:
        infos = self.api.call("VDI.get_nbd_info", vdi_ref) or []
        if not infos:
            return None
        i = infos[0]
        if self._nbd_connect is not None:
            return self._nbd_connect(i)
        tls = None
        if i.get("cert"):
            tls = ssl.create_default_context(cadata=i["cert"])
        return NbdReader.connect_tcp(i["address"], int(i["port"]), i["exportname"], tls=tls,
                                     server_hostname=i.get("subject") or i["address"])

    def open_disk(self, snapshot: SnapshotRef, disk: DiskSpec) -> DiskReader:
        v = (snapshot.data.get("vdis") or {}).get(disk.id)
        if not v or not snapshot.id.startswith(PREFIX):
            raise LiveDiskReadRefused(f"xen: no vmigrate snapshot VDI for {disk.label}")
        mode = self.config.get("transport") or "auto"
        r = self._nbd(v["ref"]) if mode in ("auto", "nbd") else None
        if r is None:
            if mode == "nbd":
                raise ProviderError("xen: no NBD-enabled network on the host (set a network's purpose to nbd), "
                                    "or set transport: auto / export")
            r = ExportStream(lambda: self.api.export_url(v["uuid"]), disk.size_bytes, getattr(self.api, "ctx", None),
                             opener=self._export_opener)
        if r.size < disk.size_bytes:
            r.close()
            raise SafetyGateError(f"{disk.label}: exported size {r.size} < inspected {disk.size_bytes}")
        return r

    def _destroy(self, snap_ref: str, vdi_refs: list[str]) -> None:
        try:
            rec = self.api.call("VM.get_record", snap_ref)
        except XapiError as e:
            if e.code == "HANDLE_INVALID":
                return
            raise
        if not rec.get("is_a_snapshot") or not str(rec.get("name_label", "")).startswith(PREFIX):
            raise SafetyGateError(f"refusing to destroy {snap_ref}: not a vmigrate snapshot")
        for v in vdi_refs:
            try:
                self.api.call("VDI.destroy", v)
            except XapiError as e:
                if e.code != "HANDLE_INVALID":
                    raise
        self.api.call("VM.destroy", snap_ref)

    def cleanup(self, snapshot: SnapshotRef) -> None:
        self.ctx.require_execute("xen destroy snapshot", name=snapshot.id)
        self._destroy(snapshot.data["snapshot_ref"], [v["ref"] for v in (snapshot.data.get("vdis") or {}).values()])

    # ------------------------------------------------------------------ CBT
    def supports_cbt(self) -> bool:
        return True

    def keep_for_cbt(self) -> bool:
        return False      # retire_snapshot keeps only the CBT metadata of the snapshot

    def checkpoint(self, snapshot: SnapshotRef) -> CheckpointRef:
        return CheckpointRef(id=snapshot.id, data={"snapshot_ref": snapshot.data["snapshot_ref"],
                                                   "vdis": dict(snapshot.data.get("vdis") or {}),
                                                   "cbt": dict(snapshot.data.get("cbt") or {})})

    def retire_snapshot(self, snapshot: SnapshotRef, previous: CheckpointRef | None) -> CheckpointRef:
        cp = self.checkpoint(snapshot)
        if any(cp.data["cbt"].values()):
            for did, v in cp.data["vdis"].items():
                if cp.data["cbt"].get(did):
                    self.api.call("VDI.data_destroy", v["ref"])      # keep only the CBT metadata
                else:
                    self.api.call("VDI.destroy", v["ref"])
        else:
            self.cleanup(snapshot)
        if previous is not None and previous.data.get("snapshot_ref") and previous.id != cp.id:
            self.release_checkpoint(previous)
        return cp

    def release_checkpoint(self, checkpoint: CheckpointRef) -> None:
        self._destroy(checkpoint.data["snapshot_ref"],
                      [v["ref"] for d, v in (checkpoint.data.get("vdis") or {}).items() if checkpoint.data["cbt"].get(d)])

    def changed_regions(self, disk: DiskSpec, base: CheckpointRef, cur: CheckpointRef) -> list[ByteRange]:
        a, b = (base.data.get("vdis") or {}).get(disk.id), (cur.data.get("vdis") or {}).get(disk.id)
        if not a or not b or not base.data.get("cbt", {}).get(disk.id) or not cur.data.get("cbt", {}).get(disk.id):
            raise ChangeTrackingUnavailable(f"{disk.label}: CBT is not enabled on this disk (enable_cbt)")
        try:
            bitmap = self.api.call("VDI.list_changed_blocks", a["ref"], b["ref"])
        except ProviderError as e:
            raise ChangeTrackingUnavailable(f"{disk.label}: VDI.list_changed_blocks: {e}") from e
        return mapping.bitmap_to_ranges(bitmap, disk.size_bytes)
