"""Purpose: Azure TargetProvider, two write modes:
  helper_vm (Coriolis' documented approach): empty managed disks are attached to a temporary Linux helper VM in
            Azure and written over SSH (NBD agent); replicas (rewritten in place), restore points (incremental
            snapshots) and OSMorph (over NBD from this worker) all work. Needs SSH from the worker to the helper.
  upload    : each disk is uploaded into a new managed disk (createOption Upload, write SAS, Put Page, VHD footer).
            HTTPS to Azure only; migrations only (an uploaded disk is final) and no OSMorph.
  auto (default): helper_vm when helper_subnet is set, else upload.

Usage (endpoint config; the same endpoint can be a source too. client_secret is a credential):
    type: azure
    subscription_id / auth / tenant_id / client_id / client_secret / cloud     # as for the source
    location: westeurope
    target_resource_group: rg-migrated          # existing resource group for disks, NICs and VMs
    network_map: {"VLAN-120": "/subscriptions/../subnets/app"}   # source network -> subnet resource ID
    default_subnet: /subscriptions/.../subnets/app
    nsg_id: null                                # optional network security group for the NICs
    vm_size: null                               # else the smallest Standard_D*s_v5 with enough vCPU / RAM
    disk_sku: Premium_LRS                       # Premium_LRS | StandardSSD_LRS | Standard_LRS | PremiumV2 is not uploadable
    sas_hours: 24
    write_mode: auto                            # auto | helper_vm | upload
    helper_subnet: /subscriptions/.../subnets/migration   # helper_vm: subnet for the helper VM
    helper_public_ip: false                     # helper_vm: give the helper a public IP (else its private IP)
    helper_nsg_id: null                         # helper_vm: NSG allowing TCP 22 from the worker
    helper_size: Standard_D4s_v5                # helper_vm: 8 data disks; pick a size that fits the VM's disk count
    helper_image: {publisher: Canonical, offer: ubuntu-24_04-lts, sku: server, version: latest}   # or an image ID
    staging_dir: /var/lib/vmigrate/staging      # helper_vm: holds the helper's SSH keys

How it works (helper_vm):
  disk     : PUT Microsoft.Compute/disks/vmigrate-<name> {createOption: Empty}; attached to the helper at the next
             free LUN and written over SSH (zero chunks skipped: an empty managed disk reads as zeros)
  replica  : the same disks are rewritten in place on later syncs; restore points are incremental snapshots
  deploy / migration: every disk becomes a NEW disk created from a snapshot (the boot disk gets osType and
             Gen1/Gen2 there); OSMorph runs on those over NBD while they are attached to the helper; the helper
             releases them and the VM attaches them. A migration then deletes its written disks.

How it works (upload):
  disk     : PUT Microsoft.Compute/disks/vmigrate-<name> {createOption: Upload, uploadSizeBytes: size + 512}; the
             boot disk gets osType and hyperVGeneration (V2 for UEFI guests) at creation
  write    : beginGetAccess Write -> Put Page (<= 4 MiB, parallel; zero chunks skipped) -> VHD footer ->
             endGetAccess (azure/upload.py). An interrupted upload recreates the disk (the SAS is not kept).
  verify   : read back through a short-lived read SAS, revoked before the VM attaches the disk
  VM       : one NIC per source NIC in the mapped subnet, PUT virtualMachines with the OS disk and data disks
             attached (createOption Attach, LUN = source order), boot diagnostics on; the VM starts
  replica  : not supported in upload mode (Azure accepts uploads only into a disk that was never finalised)
  OSMorph  : not possible in upload mode (the disks are never block-accessible): the guest must already have the
             Hyper-V drivers and agent; see osmorph/cloud.py (checked at preflight, before any upload)
Safety: only resources tagged vmigrate:managed=true are deleted. Tested against a fake ARM only.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...core import cancel
from ...core.context import RunContext
from ...core.errors import ConfigError, ProviderError, SafetyGateError
from ...core.interfaces import BootCheck, DiskHints, DiskReader, DiskWriter, TargetDiskRef, TargetProvider, VmRef
from ...ir import DiskBus, VmSpec
from ..vhi.nbd import NbdClient, NbdDiskReader, NbdDiskWriter, UnixBridge, agent_command
from .blob import SasSnapshotReader
from .client import COMPUTE_API, DISK_API, NETWORK_API
from .helper import AzureHelper
from .upload import PageBlobWriter, put_footer

MIB = 1 << 20
GIB = 1 << 30
MANAGED = "vmigrate:managed"
SIZE_RE = re.compile(r"^Standard_D(\d+)s_v5$")
FAIL = ("Kernel panic", "dracut-initqueue timeout", "Entering emergency mode", "VFS: Unable to mount root",
        "INACCESSIBLE_BOOT_DEVICE")


def disk_name(name: str) -> str:
    return ("vmigrate-" + re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-."))[:80]


def pick_size(sizes: list[dict[str, Any]], cores: int, mem_mb: int) -> str:
    fits = [s for s in sizes if SIZE_RE.match(s.get("name", "")) and s.get("numberOfCores", 0) >= cores
            and s.get("memoryInMB", 0) >= mem_mb]
    if not fits:
        raise ConfigError(f"azure: no Standard_D*s_v5 size has {cores} vCPUs and {mem_mb} MiB here; set vm_size")
    return min(fits, key=lambda s: (s["numberOfCores"], s["memoryInMB"]))["name"]


def disk_body(location: str, size: int, sku: str, hints: DiskHints, label: str) -> dict[str, Any]:
    props: dict[str, Any] = {"creationData": {"createOption": "Upload", "uploadSizeBytes": size + 512}}
    if hints.is_boot:
        props["osType"] = "Windows" if hints.os_family == "windows" else "Linux"
        props["hyperVGeneration"] = "V2" if hints.firmware == "uefi" else "V1"
    return {"location": location, "sku": {"name": sku}, "properties": props,
            "tags": {MANAGED: "true", "vmigrate:disk": label[:256]}}


def vm_body(spec: VmSpec, location: str, size: str, os_disk: str, data_disks: list[str], nics: list[str],
            os_type: str) -> dict[str, Any]:
    return {"location": location, "tags": {MANAGED: "true", "vmigrate:source": spec.source_id[:256]},
            "properties": {
                "hardwareProfile": {"vmSize": size},
                "storageProfile": {"osDisk": {"osType": os_type, "createOption": "Attach",
                                              "managedDisk": {"id": os_disk}},
                                   "dataDisks": [{"lun": i, "createOption": "Attach", "managedDisk": {"id": d}}
                                                 for i, d in enumerate(data_disks)]},
                "networkProfile": {"networkInterfaces": [{"id": n, "properties": {"primary": i == 0}}
                                                         for i, n in enumerate(nics)]},
                "diagnosticsProfile": {"bootDiagnostics": {"enabled": True}}}}


class AzureTarget(TargetProvider):
    type_name = "azure"
    platform_family = "azure"

    def __init__(self, name: str, config: Mapping[str, Any], ctx: RunContext, client: Any = None, http: Any = None):
        super().__init__(name, config, ctx)
        self._client, self._http = client, http
        self._open: dict[str, tuple[str, PageBlobWriter]] = {}
        self.poll = float(config.get("poll_interval", 10))
        self.write_mode_cfg = str(config.get("write_mode", "auto") or "auto")
        if self.write_mode_cfg not in ("auto", "helper_vm", "upload"):
            raise ConfigError("azure target: write_mode must be auto, helper_vm or upload")
        self._helper: AzureHelper | None = None
        self._helper_kw: dict[str, Any] = {}           # tests inject transport_factory / keygen
        self._bridges: dict[str, UnixBridge] = {}
        self._sockdir: str | None = None

    @property
    def write_mode(self) -> str:
        if self.write_mode_cfg != "auto":
            return self.write_mode_cfg
        return "helper_vm" if self.config.get("helper_subnet") else "upload"

    @property
    def helper(self) -> AzureHelper:
        if self._helper is None:
            self._helper = AzureHelper(self.client, self.config, self.ctx,
                                       Path(self.config.get("staging_dir", "/var/lib/vmigrate/staging")),
                                       rg_path=self.rg, location=self.location, poll=self.poll, **self._helper_kw)
        return self._helper

    def _agent(self, disk: TargetDiskRef, *, writable: bool) -> NbdClient:
        dev = self.helper.attach(disk.id)
        return NbdClient.over_process(self.helper.transport.popen(agent_command(dev, writable=writable)))

    def _close_bridges(self) -> None:
        for b in self._bridges.values():
            b.close()
        self._bridges.clear()
        if self._sockdir:
            shutil.rmtree(self._sockdir, ignore_errors=True)
            self._sockdir = None

    def _release_helper_disks(self) -> None:
        self._close_bridges()
        if self._helper is not None:
            self._helper.detach_all()

    def prepare_helper(self) -> str | None:
        if self.write_mode != "helper_vm":
            return None
        self.helper.ensure()
        return f"helper VM {self.helper.name} ready at {self.helper.host}"

    def remove_helper(self) -> str | None:
        if self._helper is None or self._helper.transport is None:
            return None
        name = self._helper.name
        self.close()
        return f"helper VM {name} removed"

    def close(self) -> None:
        if self._helper is None:
            return
        try:
            self._release_helper_disks()
        finally:
            self._helper.destroy()
            self._helper = None

    @property
    def client(self) -> Any:
        if self._client is None:
            from .client import AzureClient
            c = self.config
            self._client = AzureClient(subscription_id=c["subscription_id"], tenant_id=c.get("tenant_id"),
                                       client_id=c.get("client_id"), client_secret=c.get("client_secret"),
                                       auth=c.get("auth", "client_secret"), cloud=c.get("cloud", "public"),
                                       managed_identity_client_id=c.get("managed_identity_client_id"))
        return self._client

    @property
    def http(self) -> Any:
        return self._http or self.client.http

    @property
    def rg(self) -> str:
        rg = self.config.get("target_resource_group")
        if not rg:
            raise ConfigError("azure target: set target_resource_group")
        return f"/subscriptions/{self.client.sub}/resourceGroups/{rg}"

    @property
    def location(self) -> str:
        loc = self.config.get("location")
        if not loc:
            raise ConfigError("azure target: set location (e.g. westeurope)")
        return str(loc)

    def _sleep(self, s: float) -> None:
        cancel.sleep(s)

    def _put(self, path: str, body: dict[str, Any], api: str, what: str) -> dict[str, Any]:
        st, h, d = self.client.request("PUT", path, api=api, body=body, ok=(200, 201, 202))
        if st in (201, 202) and (h.get("azure-asyncoperation") or h.get("location")):
            self.client.wait_async(h, what)
            return self.client.get(path, api)
        return d

    def _delete(self, path: str, api: str, what: str) -> None:
        st, h, _ = self.client.request("DELETE", path, api=api, ok=(200, 202, 204, 404))
        if st == 202:
            self.client.wait_async(h, what)

    def _ours(self, path: str, api: str) -> dict[str, Any] | None:
        r = self.client.get_or_none(path, api)
        if r is not None and (r.get("tags") or {}).get(MANAGED) != "true":
            raise SafetyGateError(f"refusing to touch {path}: not tagged {MANAGED}")
        return r

    # ------------------------------------------------------------------ checks + lists
    def check_connection(self) -> list[str]:
        try:
            self.client.subscription()
            rg = self.client.get_or_none(self.rg, "2022-09-01")
        except Exception as e:  # noqa: BLE001
            return [f"cannot use the Azure API: {e}"]
        p = [] if rg else [f"resource group {self.config.get('target_resource_group')!r} not found"]
        if not self.config.get("location"):
            p.append("set location (the Azure region for the new VMs)")
        return p

    def preflight(self, spec: VmSpec | None = None) -> list[str]:
        p = self.check_connection()
        if spec is None or p:
            return p
        from ...osmorph.cloud import cloud_gate
        if self.write_mode == "helper_vm":
            p += self.helper.problems()          # OSMorph edits the guest over the helper: no prerequisite gate
        else:
            p += cloud_gate(spec, "azure")
        o = self._opts(spec)
        for nic in spec.nics or []:
            sn = (o.get("network_map") or {}).get(nic.network) or o.get("default_subnet")
            if not sn:
                p.append(f"no network_map entry (subnet ID) for source network {nic.network!r} and no default_subnet")
            elif self.client.get_or_none(sn, NETWORK_API) is None:
                p.append(f"subnet {sn} (for {nic.network!r}) not found")
        if not spec.nics and not o.get("default_subnet"):
            p.append("set default_subnet: the VM has no NIC to map")
        return p

    def _opts(self, spec: VmSpec) -> dict[str, Any]:
        t = spec.extra.get("target_options") or {}
        return {**self.config, **t, "network_map": {**(self.config.get("network_map") or {}), **(t.get("network_map") or {})}}

    def list_networks(self) -> list[dict[str, Any]]:
        out = []
        for vnet in self.client.list(f"/subscriptions/{self.client.sub}/providers/Microsoft.Network/virtualNetworks",
                                     NETWORK_API):
            for sn in (vnet.get("properties") or {}).get("subnets") or []:
                out.append({"id": sn["id"], "name": f"{vnet['name']}/{sn['name']} "
                                                   f"({(sn.get('properties') or {}).get('addressPrefix', '')})"})
        return sorted(out, key=lambda s: s["name"])

    def list_storage(self) -> list[dict[str, Any]]:
        return [{"id": s, "name": s} for s in ("Premium_LRS", "StandardSSD_LRS", "Standard_LRS", "Premium_ZRS",
                                               "StandardSSD_ZRS")]

    # ------------------------------------------------------------------ disks
    def _disk_path(self, name: str) -> str:
        return f"{self.rg}/providers/Microsoft.Compute/disks/{disk_name(name)}"

    def create_disk(self, name: str, size_bytes: int, hints: DiskHints) -> TargetDiskRef:
        if hints.shared:
            raise ConfigError("azure target: shared cluster disks are not supported")
        sku = hints.storage_pool or str(self.config.get("disk_sku", "Premium_LRS"))
        path = self._disk_path(name)
        boot = {"is_boot": hints.is_boot, "firmware": hints.firmware, "os_family": hints.os_family}
        if self.write_mode == "helper_vm":
            gib = max(1, -(-size_bytes // GIB))
            body = {"location": self.location, "sku": {"name": sku},
                    "properties": {"creationData": {"createOption": "Empty"}, "diskSizeGB": gib},
                    "tags": {MANAGED: "true", "vmigrate:disk": name[:256]}}
            self.ctx.require_execute("azure create empty disk", disk=disk_name(name), size_gib=gib, sku=sku)
            d = self._put(path, body, DISK_API, f"create disk {disk_name(name)}")
            return TargetDiskRef(id=str(d.get("id") or path), name=name, size_bytes=gib * GIB,
                                 data={"path": path, "mode": "helper_vm", "sku": sku, **boot})
        size = -(-size_bytes // MIB) * MIB
        body = disk_body(self.location, size, sku, hints, name)
        self.ctx.require_execute("azure create upload disk", disk=disk_name(name), size_bytes=size, sku=sku)
        d = self._put(path, body, DISK_API, f"create disk {disk_name(name)}")
        return TargetDiskRef(id=str(d.get("id") or path), name=name, size_bytes=size,
                             data={"path": path, "body": body, "os_type": body["properties"].get("osType"),
                                   "mode": "upload", **boot})

    def find_disk(self, name: str) -> TargetDiskRef | None:
        d = self._ours(self._disk_path(name), DISK_API)
        if d is None:
            return None
        size = int((d.get("properties") or {}).get("diskSizeBytes") or
                   ((d.get("properties") or {}).get("creationData") or {}).get("uploadSizeBytes", 512) - 512)
        mode = "upload" if ((d.get("properties") or {}).get("creationData") or {}).get("createOption") == "Upload" \
            else "helper_vm"
        return TargetDiskRef(id=str(d["id"]), name=name, size_bytes=size,
                             data={"path": self._disk_path(name), "os_type": (d.get("properties") or {}).get("osType"),
                                   "mode": mode})

    def _grant(self, path: str, access: str) -> str:
        hours = float(self.config.get("sas_hours", 24))
        _, h, d = self.client.request("POST", f"{path}/beginGetAccess", api=DISK_API,
                                      body={"access": access, "durationInSeconds": int(hours * 3600)}, ok=(200, 202))
        if not d.get("accessSAS"):
            d = self.client.wait_async(h, f"grant {access} access")
        sas = d.get("accessSAS") or ((d.get("properties") or {}).get("output") or {}).get("accessSAS")
        if not sas:
            raise ProviderError(f"azure: beginGetAccess ({access}) on {path} returned no SAS")
        return str(sas)

    def _revoke(self, path: str) -> None:
        _, h, _ = self.client.request("POST", f"{path}/endGetAccess", api=DISK_API, ok=(200, 202))
        self.client.wait_async(h, "end disk access")

    def _state(self, path: str) -> str:
        d = self.client.get_or_none(path, DISK_API) or {}
        return str((d.get("properties") or {}).get("diskState") or "")

    def open_disk_writer(self, disk: TargetDiskRef, *, resume: bool = False, incremental: bool = False) -> DiskWriter:
        if disk.data.get("mode") == "helper_vm":
            self.ctx.require_execute("azure write disk through helper VM", disk=disk.name, incremental=incremental)
            return NbdDiskWriter(self._agent(disk, writable=True), disk.size_bytes, prezeroed=not incremental)
        if incremental:
            raise ProviderError("azure: an uploaded managed disk cannot be resynced; replicas are not supported")
        path = disk.data["path"]
        self._ours(path, DISK_API)
        st = self._state(path)
        if st != "ReadyToUpload":              # an interrupted upload: start over on a fresh disk of the same name
            self.ctx.logger.info("azure: %s is %s; recreating it for a fresh upload", disk.name, st or "missing")
            if st == "ActiveUpload":
                self._revoke(path)
            self._delete(path, DISK_API, "delete stale upload disk")
            self._put(path, disk.data.get("body") or disk_body(
                self.location, disk.size_bytes, str(self.config.get("disk_sku", "Premium_LRS")),
                DiskHints(bus=DiskBus.SCSI), disk.name), DISK_API, "recreate upload disk")
        self.ctx.require_execute("azure upload disk", disk=disk_name(disk.name))
        sas = self._grant(path, "Write")
        w = PageBlobWriter(self.http, sas, disk.size_bytes, workers=int(self.config.get("write_workers", 8)),
                           sleep=self._sleep)
        self._open[disk.id] = (sas, w)
        return w

    def commit_disk(self, disk: TargetDiskRef, writer_staged: Path | None) -> None:
        if disk.data.get("mode") == "helper_vm":
            return                                   # the writer flushed straight onto the attached disk
        if disk.id not in self._open:
            raise ProviderError(f"azure: {disk.name}: nothing was uploaded in this run")
        sas, w = self._open.pop(disk.id)
        w.close()
        put_footer(self.http, sas, disk.size_bytes, sleep=self._sleep)
        self._revoke(disk.data["path"])          # ends the upload: the disk becomes Unattached
        if self._state(disk.data["path"]) not in ("Unattached", ""):
            raise ProviderError(f"azure: {disk.name} did not finish uploading ({self._state(disk.data['path'])})")

    def open_disk_reader(self, disk: TargetDiskRef) -> DiskReader | None:
        if not self.config.get("verify_after_commit", True):
            return None
        if disk.data.get("mode") == "helper_vm":
            return NbdDiskReader(self._agent(disk, writable=False), disk.size_bytes)
        path = disk.data["path"]
        return SasSnapshotReader(lambda renew: self._grant(path, "Read"), disk.size_bytes, http=self.http,
                                 sleep=self._sleep, on_close=lambda: self._revoke(path))

    def morph_image(self, disk: TargetDiskRef) -> tuple[Path, str] | None:
        if disk.data.get("mode") != "helper_vm":
            return None
        if disk.id not in self._bridges:
            dev = self.helper.attach(disk.id)
            t = self.helper.transport
            if self._sockdir is None:
                self._sockdir = tempfile.mkdtemp(prefix="vmaz-", dir="/tmp" if os.path.isdir("/tmp") else None)
            self._bridges[disk.id] = UnixBridge(Path(self._sockdir) / f"{len(self._bridges)}.sock",
                                                lambda: t.popen(agent_command(dev, writable=True)))
        return (self._bridges[disk.id].uri, "raw")  # type: ignore[return-value]

    def morph_options(self) -> dict[str, Any]:
        return {"azure_disks_editable": self.write_mode == "helper_vm"}

    # ------------------------------------------------------------------ replica + restore points (helper_vm)
    def supports_replica(self) -> bool:
        return self.write_mode == "helper_vm"

    def supports_restore_points(self) -> tuple[bool, str]:
        if self.write_mode == "helper_vm":
            return True, ""
        return False, "azure: restore points need write_mode helper_vm"

    def _snapshot(self, disk: TargetDiskRef, name: str, *, rp: str | None = None) -> str:
        path = f"{self.rg}/providers/Microsoft.Compute/snapshots/{disk_name(name)[:80]}"
        tags = {MANAGED: "true", "vmigrate:disk": disk.name[:256]}
        if rp:
            tags["vmigrate:rp"] = rp[:256]
        self.ctx.require_execute("azure incremental snapshot", disk=disk.name, snapshot=path.rsplit("/", 1)[-1])
        self._put(path, {"location": self.location, "tags": tags,
                         "properties": {"creationData": {"createOption": "Copy", "sourceResourceId": disk.id},
                                        "incremental": True}}, DISK_API, "create snapshot")
        return str(self.client.wait_snapshot(path).get("id") or path)

    def snapshot_disk(self, disk: TargetDiskRef, name: str) -> str:
        return self._snapshot(disk, f"{disk.name}-{name}", rp=name)

    def delete_disk_snapshot(self, disk: TargetDiskRef, snapshot_id: str) -> None:
        if self._ours(snapshot_id, DISK_API) is not None:
            self._delete(snapshot_id, DISK_API, "delete snapshot")

    def clone_disk(self, disk: TargetDiskRef, name: str, restore_point: str | None = None) -> TargetDiskRef:
        """A NEW disk from a snapshot of `disk` (a restore point's, or a fresh one). The boot disk gets its
        osType and Gen1/Gen2 here, which an empty disk cannot carry."""
        if disk.data.get("mode") != "helper_vm":
            raise ProviderError("azure: deploy needs write_mode helper_vm (upload mode has no replicas)")
        self.helper.detach(disk.id)
        temp = restore_point is None
        snap = restore_point or self._snapshot(disk, f"{name}-src")
        path = self._disk_path(name)
        props: dict[str, Any] = {"creationData": {"createOption": "Copy", "sourceResourceId": snap}}
        if disk.data.get("is_boot"):
            props["osType"] = "Windows" if disk.data.get("os_family") == "windows" else "Linux"
            props["hyperVGeneration"] = "V2" if disk.data.get("firmware") == "uefi" else "V1"
        self.ctx.require_execute("azure create disk from snapshot", disk=disk_name(name), source=snap.rsplit("/", 1)[-1])
        d = self._put(path, {"location": self.location, "sku": {"name": disk.data.get("sku") or
                                                                  self.config.get("disk_sku", "Premium_LRS")},
                             "properties": props, "tags": {MANAGED: "true", "vmigrate:disk": name[:256]}},
                      DISK_API, f"create disk {disk_name(name)}")
        if temp:
            self._delete(snap, DISK_API, "delete temporary snapshot")
        return TargetDiskRef(id=str(d.get("id") or path), name=name, size_bytes=disk.size_bytes,
                             data={**disk.data, "path": path, "clone": True, "os_type": props.get("osType")})

    def delete_disk(self, disk: TargetDiskRef) -> None:
        path = disk.data["path"]
        d = self._ours(path, DISK_API)
        if d is None:
            return
        self.ctx.require_execute("azure delete disk", disk=path.rsplit("/", 1)[-1])
        if self._helper is not None:
            self._helper.detach(disk.id)
        if (d.get("properties") or {}).get("diskState") in ("ActiveUpload", "ActiveSAS"):
            self._revoke(path)
        self._delete(path, DISK_API, "delete disk")

    # ------------------------------------------------------------------ VM
    def target_bus(self, spec: VmSpec) -> DiskBus:
        return DiskBus.SCSI

    def _vm_path(self, name: str) -> str:
        return f"{self.rg}/providers/Microsoft.Compute/virtualMachines/{name}"

    def _nic_path(self, vm: str, i: int) -> str:
        return f"{self.rg}/providers/Microsoft.Network/networkInterfaces/{vm}-nic{i}"

    def create_vm(self, spec: VmSpec, boot_disk: TargetDiskRef, data_disks: list[TargetDiskRef]) -> VmRef:
        written: list[TargetDiskRef] = []
        boot_of: str | None = None
        if boot_disk.data.get("mode") == "helper_vm" and not boot_disk.data.get("clone"):
            # migration: the written boot disk has no osType / generation; give the VM a copy that has them
            written, boot_of = [boot_disk], boot_disk.id
            boot_disk = self.clone_disk(boot_disk, f"{boot_disk.name}-os")
        self._release_helper_disks()             # the helper lets go of every disk before the VM attaches them
        vm = self._create_vm(spec, boot_disk, data_disks, boot_of=boot_of)
        for d in written:
            self.delete_disk(d)                  # the VM has its own copy now
        return vm

    def _create_vm(self, spec: VmSpec, boot_disk: TargetDiskRef, data_disks: list[TargetDiskRef], *,
                   boot_of: str | None = None) -> VmRef:
        o = self._opts(spec)
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", spec.name):
            raise ConfigError(f"azure: {spec.name!r} is not a valid VM name; set target_name")
        subnets = [(o.get("network_map") or {}).get(n.network) or o.get("default_subnet") for n in spec.nics] \
            or [o.get("default_subnet")]
        if not all(subnets):
            raise ConfigError("azure: a source network has no subnet in network_map and there is no default_subnet")
        nics = []
        for i, sn in enumerate(subnets):
            body: dict[str, Any] = {"location": self.location, "tags": {MANAGED: "true"},
                                    "properties": {"ipConfigurations": [{"name": "ipconfig1", "properties": {
                                        "subnet": {"id": sn}, "privateIPAllocationMethod": "Dynamic",
                                        "primary": True}}]}}
            if o.get("nsg_id"):
                body["properties"]["networkSecurityGroup"] = {"id": o["nsg_id"]}
            self.ctx.require_execute("azure create NIC", vm=spec.name, subnet=sn)
            nics.append(str(self._put(self._nic_path(spec.name, i), body, NETWORK_API, "create NIC").get("id")
                            or self._nic_path(spec.name, i)))
        size = o.get("vm_size") or pick_size(
            self.client.list(f"/subscriptions/{self.client.sub}/providers/Microsoft.Compute/locations/{self.location}"
                             "/vmSizes", COMPUTE_API), spec.cpu.total, spec.memory_bytes >> 20)
        os_type = boot_disk.data.get("os_type") or "Linux"
        body = vm_body(spec, self.location, size, boot_disk.id, [d.id for d in data_disks], nics, os_type)
        if boot_of:                               # the engine knows the boot disk by the disk it wrote
            body["tags"]["vmigrate:boot-of"] = boot_of[:256]
        self.ctx.require_execute("azure create VM", name=spec.name, size=size, disks=1 + len(data_disks))
        vm = self._put(self._vm_path(spec.name), body, COMPUTE_API, f"create VM {spec.name}")
        return VmRef(str(vm.get("id") or self._vm_path(spec.name)), spec.name, self.name)

    def find_vm(self, name: str) -> VmRef | None:
        vm = self.client.get_or_none(self._vm_path(name), COMPUTE_API)
        return VmRef(str(vm["id"]), name, self.name) if vm else None

    def attach_disk(self, vm: VmRef, disk: TargetDiskRef, order: int) -> None:
        raise ProviderError("azure: every disk is attached when the VM is created")

    def list_attached_disks(self, vm: VmRef) -> list[str]:
        v = self.client.get_or_none(vm.id, COMPUTE_API) or {}
        sp = (v.get("properties") or {}).get("storageProfile") or {}
        os_id = ((sp.get("osDisk") or {}).get("managedDisk") or {}).get("id")
        data = sorted(sp.get("dataDisks") or [], key=lambda d: d.get("lun", 0))
        os_id = (v.get("tags") or {}).get("vmigrate:boot-of") or os_id
        return [x for x in [os_id, *[(d.get("managedDisk") or {}).get("id") for d in data]] if x]

    def check_boot(self, vm: VmRef, timeout: float = 900, *, guest_os: str | None = None,
                   progress: Any = None) -> BootCheck:
        """The Azure VM agent reporting Ready = booted. Without the agent the VM passes after `boot_grace`
        seconds running (default 300) unless the serial log (boot diagnostics) shows a known failure."""
        say = progress or self.ctx.logger.info
        grace = float(self.config.get("boot_grace", 300))
        start, last = time.monotonic(), -1e9
        while time.monotonic() - start < timeout:
            iv = self.client.get(f"{vm.id}/instanceView", COMPUTE_API)
            power = next((s["code"].split("/", 1)[1] for s in iv.get("statuses") or []
                          if str(s.get("code", "")).startswith("PowerState/")), "")
            agent = next((s.get("displayStatus") for s in (iv.get("vmAgent") or {}).get("statuses") or []), "")
            el = time.monotonic() - start
            if power in ("stopped", "deallocated") and el > 30:
                return BootCheck(False, f"VM is {power}")
            if agent == "Ready":
                return BootCheck(True, "the Azure VM agent reports Ready")
            log = self._serial(vm.id) if el >= 60 else ""
            for pat in FAIL:
                if pat in log:
                    return BootCheck(False, f"guest boot failure: {pat!r} in the serial log", log[-2000:])
            if power == "running" and el >= grace:
                return BootCheck(True, f"running for {int(el)}s; no VM agent status (not installed?). Confirm in the "
                                       "Azure serial console / boot diagnostics", log[-2000:])
            if el - last >= 60:
                last = el
                say(f"boot check: VM {power or '?'}, agent {agent or 'not reporting'}, {int(el)}s of {int(timeout)}s")
            self._sleep(self.poll)
        return BootCheck(False, f"VM not confirmed booted within {int(timeout)}s")

    def _serial(self, vm_id: str) -> str:
        try:
            _, _, d = self.client.request("POST", f"{vm_id}/retrieveBootDiagnosticsData?sasUriExpirationTimeInMinutes=5",
                                          api=COMPUTE_API, ok=(200,))
            uri = d.get("serialConsoleLogBlobUri")
            if not uri:
                return ""
            st, _, body = self.http("GET", uri, {}, None, 30)
            return body.decode(errors="replace") if st == 200 else ""
        except Exception:  # noqa: BLE001 - the serial log is a hint only
            return ""

    # ------------------------------------------------------------------ cleanup
    def cleanup_on_failure(self, created: Mapping[str, Any]) -> None:
        self.ctx.require_execute("azure cleanup created resources")
        try:
            self.close()                          # the helper first: its attachments block disk deletion
        except Exception as e:  # noqa: BLE001 - keep cleaning; report
            self.ctx.logger.warning("azure: helper cleanup: %s", e)
        for _, w in list(self._open.values()):
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass
        self._open.clear()
        vm = created.get("target_vm")
        if vm and self._ours(vm["id"], COMPUTE_API) is not None:
            self._delete(vm["id"], COMPUTE_API, "delete VM")
        # NICs are created before the VM: clean them for a VM that failed to create, too
        for name in {n for n in ((vm or {}).get("name"), created.get("target_vm_pending")) if n}:
            i = 0
            while self._ours(self._nic_path(name, i), NETWORK_API) is not None:
                self._delete(self._nic_path(name, i), NETWORK_API, "delete NIC")
                i += 1
        for d in created.get("target_disks", []):
            data = d.get("data") or {}
            if data.get("path"):
                self.delete_disk(TargetDiskRef(d["id"], d["name"], d["size_bytes"], data=data))
