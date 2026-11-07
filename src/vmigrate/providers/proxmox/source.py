"""Purpose: Proxmox VE SourceProvider (qemu VMs; containers are not VMs and are not listed).

Usage (endpoint config; token secret + SSH key go in the encrypted credentials):
    type: proxmox
    url: https://pve1.example.com:8006
    token_id: migrate@pve!vmigrate             # API token (VM.Audit, VM.Snapshot, Datastore.Audit, Sys.Audit)
    token_secret: ...                          # credential
    verify_tls: true
    ssh_user: root                             # or a sudo-capable user (sudo -n)
    ssh_private_key: "-----BEGIN OPENSSH PRIVATE KEY-----..."   # credential
    ssh_known_hosts: "10.0.0.11 ssh-ed25519 AAAA...\\n10.0.0.12 ssh-ed25519 AAAA..."   # pinned host keys
    node_addresses: {pve1: 10.0.0.11, pve2: 10.0.0.12}   # default: the node name resolves

How it works:
  inspect  : /cluster/resources + /nodes/{node}/qemu/{vmid}/config -> IR (controller from scsihw)
  gate     : config disks vs the storages' content listing for the vmid (minus unused/EFI/TPM/
             cloud-init), plus "no pending disk changes" (a pending hotplug fails loud)
  refuse   : pass-through disks, storages without snapshots, raw files on dir/nfs, lxc containers
  snapshot : POST .../snapshot snapname=vmigrate-<run> (vmstate off). With the guest agent enabled
             PVE freezes the guest filesystems (app-consistent); app_consistent without agent=1 is
             refused rather than silently crash-consistent.
  read     : over SSH on the owning node: lvmthin -> activate snap LV; zfs -> read-only clone of
             @snap; rbd -> rbd:pool/image@snap; qcow2 -> internal snapshot (--load-snapshot,
             --force-share). qemu-nbd --read-only serves it; OpenSSH forwards the socket here.
  CBT      : Ceph RBD only: `rbd diff --from-snap <prev> image@cur`; the previous vmigrate
             snapshot is kept as the diff base. Other storages: replicas use compare mode.
  cleanup  : DELETE .../snapshot/vmigrate-* that this run created; temporary clones/LV activations
             are undone when each disk read finishes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ...core.context import RunContext
from ...core.errors import ChangeTrackingUnavailable, ConfigError, LiveDiskReadRefused, ProviderError, SafetyGateError
from ...core.interfaces import CheckpointRef, DiskReader, SnapshotRef, SourceProvider, VmRef
from ...core.ranges import ByteRange, normalize
from ...ir import DiskSpec, VmSpec
from . import mapping
from .api import SNAP_PREFIX


class ProxmoxSource(SourceProvider):
    type_name = "proxmox"

    def __init__(self, name: str, config: Mapping[str, Any], ctx: RunContext, api: Any = None,
                 shell_factory: Any = None, export_factory: Any = None):
        super().__init__(name, config, ctx)
        for k in ("url", "token_id"):
            if not config.get(k):
                raise ConfigError(f"proxmox source {name}: missing {k!r}")
        self._api = api
        self._shell_factory = shell_factory
        self._export_factory = export_factory
        self._shells: dict[str, Any] = {}
        self._vm: dict[str, dict[str, Any]] = {}
        self._storages: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ plumbing
    @property
    def api(self) -> Any:
        if self._api is None:
            from .api import PveApi
            self._api = PveApi(self.config["url"], self.config["token_id"], self.config.get("token_secret", ""),
                               verify_tls=bool(self.config.get("verify_tls", True)))
        return self._api

    def shell(self, node: str) -> Any:
        if node not in self._shells:
            if self._shell_factory is not None:
                self._shells[node] = self._shell_factory(node)
            else:
                from .node import SshNode
                host = (self.config.get("node_addresses") or {}).get(node, node)
                self._shells[node] = SshNode(host, self.config.get("ssh_user", "root"),
                                             self.config.get("ssh_private_key", ""), self.config.get("ssh_known_hosts", ""),
                                             port=int(self.config.get("ssh_port", 22)))
        return self._shells[node]

    def close(self) -> None:
        for s in self._shells.values():
            if hasattr(s, "close"):
                s.close()
        self._shells.clear()

    def _storage(self, sid: str) -> dict[str, Any]:
        if sid not in self._storages:
            self._storages[sid] = self.api.storage(sid)
        return self._storages[sid]

    def _resource(self, vm: VmRef) -> dict[str, Any]:
        for r in self.api.vms():
            if str(r["vmid"]) == vm.id:
                return r
        raise ProviderError(f"proxmox: VM {vm.id} not found (removed or migrated?)")

    def _load(self, vm: VmRef) -> dict[str, Any]:
        res = self._resource(vm)
        node, vmid = res["node"], int(res["vmid"])
        cfg = self.api.config(node, vmid)
        pend = [p for p in self.api.pending(node, vmid) if mapping.DISK_KEY.match(p.get("key", ""))
                and ("pending" in p or p.get("delete"))]
        storages = sorted({d["storage"] for d in mapping.config_disks(cfg)})
        content: list[dict[str, Any]] = []
        for sid in storages:
            content += self.api.content(node, sid, vmid)
        sizes = {c["volid"]: int(c.get("size") or 0) for c in content}
        agent_os = self.api.agent_osinfo(node, vmid) if mapping.agent_enabled(cfg) else None
        d = {"res": res, "node": node, "vmid": vmid, "cfg": cfg, "pending": pend, "content": content, "sizes": sizes,
             "agent_os": agent_os}
        self._vm[vm.id] = d
        return d

    def _get(self, vm_id: str) -> dict[str, Any]:
        return self._vm.get(vm_id) or self._load(VmRef(vm_id, "", self.name))

    # ------------------------------------------------------------------ contract
    def check_connection(self) -> list[str]:
        try:
            self.api.version()
            self.api.vms()          # proves the token can list VMs (VM.Audit)
        except Exception as e:  # noqa: BLE001
            return [f"cannot use the Proxmox API at {self.config['url']} with this token: {e}"]
        return []

    def preflight(self, spec: VmSpec | None = None) -> list[str]:
        try:
            self.api.version()
        except Exception as e:  # noqa: BLE001
            return [f"cannot reach Proxmox API {self.config['url']}: {e}"]
        p: list[str] = []
        for k in ("ssh_private_key", "ssh_known_hosts"):
            if not self.config.get(k):
                p.append(f"{k} is not set (needed to read snapshot data on the node)")
        if spec is not None and not p:
            node = spec.extra.get("proxmox", {}).get("node")
            try:
                out = self.shell(node).run(["sh", "-c", "command -v qemu-nbd >/dev/null && echo ok"], check=False)
                if "ok" not in out:
                    p.append(f"{node}: qemu-nbd not found on the node")
            except Exception as e:  # noqa: BLE001
                p.append(f"cannot ssh to node {node}: {e}")
        return p

    def list_vms(self) -> list[VmRef]:
        return [VmRef(str(r["vmid"]), r.get("name") or f"vm-{r['vmid']}", self.name) for r in self.api.vms()]

    def find_vm(self, name_or_id: str) -> VmRef:
        vms = self.api.vms()
        hit = [r for r in vms if str(r["vmid"]) == str(name_or_id)] or [r for r in vms if r.get("name") == name_or_id]
        if not hit:
            raise ProviderError(f"proxmox: no qemu VM {name_or_id!r}")
        if len(hit) > 1:
            raise SafetyGateError(f"proxmox: {len(hit)} VMs are named {name_or_id!r}; use the vmid "
                                  f"({', '.join(str(r['vmid']) for r in hit)})")
        return VmRef(str(hit[0]["vmid"]), hit[0].get("name") or name_or_id, self.name)

    def inspect_vm(self, vm: VmRef) -> VmSpec:
        d = self._load(vm)
        if d["pending"]:
            raise SafetyGateError(f"VM {d['vmid']} has pending (not yet applied) disk changes "
                                  f"{[p['key'] for p in d['pending']]}; apply or revert them first")
        try:
            return mapping.to_vmspec(d["res"], d["cfg"], d["sizes"], d["agent_os"])
        except mapping.ProxmoxMappingError as e:
            raise SafetyGateError(str(e)) from e

    def disk_count_crosscheck(self, vm: VmRef) -> dict[str, int]:
        d = self._get(vm.id)
        return mapping.crosscheck(d["cfg"], d["content"], d["vmid"])

    def create_snapshot(self, vm: VmRef, app_consistent: bool) -> SnapshotRef:
        d = self._get(vm.id)
        for disk in mapping.config_disks(d["cfg"]):
            st = self._storage(disk["storage"])
            if st.get("type") not in mapping.SNAPSHOT_STORAGES:
                raise SafetyGateError(f"{disk['key']} is on {disk['storage']} ({st.get('type')}), which has no "
                                      "snapshots; move the disk to lvmthin/zfs/ceph or qcow2 first")
        if app_consistent and not mapping.agent_enabled(d["cfg"]):
            raise SafetyGateError(f"VM {d['vmid']}: app-consistent snapshot requested but the QEMU guest agent is "
                                  "not enabled (agent: 1). Enable it, or set app_consistent: false.")
        name = f"{SNAP_PREFIX}{self.ctx.run_id}"
        self.ctx.require_execute("proxmox snapshot", vm=vm.name, vmid=d["vmid"], name=name)
        self.api.create_snapshot(d["node"], d["vmid"], name, vmstate=False)
        return SnapshotRef(id=name, vm=vm, app_consistent=app_consistent,
                           data={"node": d["node"], "vmid": d["vmid"], "name": name})

    def snapshot_disk_count(self, snapshot: SnapshotRef) -> int | None:
        snaps = {s["name"]: s for s in self.api.snapshots(snapshot.data["node"], snapshot.data["vmid"])}
        if snapshot.id not in snaps:
            raise SafetyGateError(f"snapshot {snapshot.id} is not listed on VM {snapshot.data['vmid']}")
        return len(mapping.config_disks(self.api.get(
            f"/nodes/{snapshot.data['node']}/qemu/{snapshot.data['vmid']}/snapshot/{snapshot.id}/config")))

    def _plan(self, snapshot: SnapshotRef, disk: DiskSpec) -> dict[str, Any]:
        node = snapshot.data["node"]
        volid = mapping.safe(disk.source_handle["volid"], "volume id")
        st = self._storage(disk.source_handle["storage"])
        path = self.shell(node).run(["pvesm", "path", volid]).strip()
        fmt = "qcow2" if volid.endswith(".qcow2") else ("vmdk" if volid.endswith(".vmdk") else "raw")
        tag = f"{SNAP_PREFIX}{self.ctx.run_id}-{disk.source_handle['key']}"
        return mapping.expose_plan(volid, st.get("type", ""), path, fmt, snapshot.id, tag)

    def open_disk(self, snapshot: SnapshotRef, disk: DiskSpec) -> DiskReader:
        if not snapshot.id.startswith(SNAP_PREFIX):
            raise LiveDiskReadRefused("proxmox: reads require a vmigrate snapshot")
        try:
            plan = self._plan(snapshot, disk)
        except mapping.ProxmoxMappingError as e:
            raise SafetyGateError(str(e)) from e
        tag = f"{SNAP_PREFIX}{self.ctx.run_id}-{disk.source_handle['key']}"
        if self._export_factory is not None:
            r = self._export_factory(self.shell(snapshot.data["node"]), plan, tag, snapshot, disk)
        else:
            from .node import NodeExport
            r = NodeExport(self.shell(snapshot.data["node"]), plan, tag=tag)
        if r.size < disk.size_bytes:
            r.close()
            raise SafetyGateError(f"{disk.label}: exported size {r.size} < inspected {disk.size_bytes}")
        return r

    def cleanup(self, snapshot: SnapshotRef) -> None:
        self.ctx.require_execute("proxmox delete snapshot", vmid=snapshot.data.get("vmid"), name=snapshot.id)
        self.api.delete_snapshot(snapshot.data["node"], snapshot.data["vmid"], snapshot.id)

    # ------------------------------------------------------------------ CBT (Ceph RBD)
    def _all_rbd(self, cfg: dict[str, Any]) -> bool:
        disks = mapping.config_disks(cfg)
        return bool(disks) and all(self._storage(d["storage"]).get("type") == "rbd" for d in disks)

    def supports_cbt(self) -> bool:
        return any(self._all_rbd(v["cfg"]) for v in self._vm.values())

    def keep_for_cbt(self) -> bool:
        return self.supports_cbt()      # rbd diff needs the previous snapshot as --from-snap

    def changed_regions(self, disk: DiskSpec, base: CheckpointRef, cur: CheckpointRef) -> list[ByteRange]:
        node = cur.data["node"]
        volid = mapping.safe(disk.source_handle["volid"], "volume id")
        path = self.shell(node).run(["pvesm", "path", volid]).strip()
        if not path.startswith("rbd:"):
            raise ChangeTrackingUnavailable(f"{volid} is not on rbd")
        image = path[4:].split(":", 1)[0]
        opts = path[4:].split(":", 1)[1] if ":" in path[4:] else ""
        argv = ["rbd", "diff", "--format", "json", "--from-snap", mapping.safe(base.id, "snapshot"),
                f"{image}@{mapping.safe(cur.id, 'snapshot')}"]
        for kv in opts.split(":"):
            if kv.startswith(("conf=", "id=", "keyring=", "mon_host=")):
                k, v = kv.split("=", 1)
                argv += [{"conf": "--conf", "id": "--id", "keyring": "--keyring", "mon_host": "-m"}[k],
                         v.replace("\\;", ",")]
        try:
            out = self.shell(node).run(argv)
            entries = json.loads(out or "[]")
        except (ProviderError, ValueError) as e:
            raise ChangeTrackingUnavailable(f"rbd diff for {volid}: {e}") from e
        return normalize(ByteRange(int(x["offset"]), int(x["length"])) for x in entries)   # exists=false = zeroed
