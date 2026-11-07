"""Purpose: the narrow slice of the vSphere API the VMware source needs, behind one class. This is
the ONLY module that imports pyVmomi; it turns managed objects into the plain dicts mapping.py
understands, so all decisions stay pure and unit-testable (tests use a fake client).

Usage:
    c = VsphereClient(host, user, password, port=443, verify_tls=True)
    c.list_vms(); c.vm_info("vm-123"); snap = c.create_snapshot("vm-123", "vmigrate-x", quiesce=True)
    c.snapshot_disks("vm-123", snap); c.query_changed("vm-123", snap, 2000, start, change_id)
    c.remove_snapshot("vm-123", snap)   # refuses snapshots not named vmigrate-*

Read-only on the source except: create/remove snapshots named vmigrate-* (that this tool made).
"""

from __future__ import annotations

import hashlib
import ssl
from typing import Any

from ...core.errors import ProviderError, SafetyGateError, TransientProviderError

SNAP_PREFIX = "vmigrate-"


def _cls(obj: Any) -> str:
    return type(obj).__name__.split(".")[-1]


def server_thumbprint(host: str, port: int = 443) -> str:
    """SHA-1 thumbprint VDDK expects ('AA:BB:...'). Call only after the TLS chain was verified (or
    with an operator-pinned thumbprint), otherwise this would trust whatever answered."""
    der = ssl.PEM_cert_to_DER_cert(ssl.get_server_certificate((host, port)))
    h = hashlib.sha1(der).hexdigest().upper()  # noqa: S324 - VDDK's thumbprint format, not a security hash choice
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


class VsphereClient:
    def __init__(self, host: str, user: str, password: str, *, port: int = 443, verify_tls: bool = True):
        from pyVim.connect import SmartConnect  # provider-private dependency
        try:
            self.si = SmartConnect(host=host, user=user, pwd=password, port=port,
                                   disableSslCertValidation=not verify_tls)
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"cannot connect to vSphere {host}: {e}") from e
        self.host, self.port = host, port
        self.content = self.si.RetrieveContent()

    def close(self) -> None:
        from pyVim.connect import Disconnect
        try:
            Disconnect(self.si)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ helpers
    def _vm(self, moref: str) -> Any:
        from pyVmomi import vim
        return vim.VirtualMachine(moref, self.si._stub)

    def _wait(self, task: Any, what: str) -> Any:
        from pyVim.task import WaitForTask
        try:
            WaitForTask(task)
        except Exception as e:  # noqa: BLE001
            msg = getattr(e, "msg", None) or str(e)
            raise ProviderError(f"{what} failed: {msg}") from e
        return task.info.result

    def session_cookie(self) -> str:
        raw = self.si._stub.cookie or ""
        return raw.split(";", 1)[0].strip()

    def about(self) -> dict[str, str]:
        a = self.content.about
        return {"name": a.fullName, "api": a.apiVersion, "type": a.apiType}

    # ------------------------------------------------------------------ inventory
    def list_vms(self) -> list[dict[str, Any]]:
        from pyVmomi import vim, vmodl
        view = self.content.viewManager.CreateContainerView(self.content.rootFolder, [vim.VirtualMachine], True)
        try:
            pc = vmodl.query.PropertyCollector
            ts = pc.TraversalSpec(name="v", path="view", skip=False, type=vim.view.ContainerView)
            fs = pc.FilterSpec(objectSet=[pc.ObjectSpec(obj=view, skip=True, selectSet=[ts])],
                               propSet=[pc.PropertySpec(type=vim.VirtualMachine, all=False,
                                                        pathSet=["name", "config.template", "runtime.powerState"])])
            out = []
            res = self.content.propertyCollector.RetrievePropertiesEx([fs], pc.RetrieveOptions())
            while res:
                for o in res.objects:
                    p = {x.name: x.val for x in o.propSet}
                    if not p.get("config.template"):
                        out.append({"moref": o.obj._moId, "name": p.get("name"), "power": str(p.get("runtime.powerState"))})
                res = self.content.propertyCollector.ContinueRetrievePropertiesEx(res.token) if res.token else None
            return out
        finally:
            view.Destroy()

    def _disk_dict(self, d: Any) -> dict[str, Any]:
        b = d.backing
        parent = getattr(b, "parent", None)
        ds = getattr(b, "datastore", None)
        return {"key": d.key, "controller_key": d.controllerKey, "unit_number": d.unitNumber,
                "capacity_bytes": int(getattr(d, "capacityInBytes", 0) or (d.capacityInKB or 0) * 1024),
                "file_name": getattr(b, "fileName", None), "datastore": ds.name if ds is not None else None,
                "backing_type": _cls(b), "disk_mode": getattr(b, "diskMode", None),
                "rdm_mode": getattr(b, "compatibilityMode", None),
                "parent_file": parent.fileName if parent is not None else None,
                "uuid": getattr(b, "uuid", None), "change_id": getattr(b, "changeId", None)}

    def _devices(self, hw: Any) -> tuple[list[dict], list[dict], list[dict]]:
        from pyVmomi import vim
        ctrls, disks, nics = [], [], []
        for dev in hw.device:
            if isinstance(dev, vim.vm.device.VirtualDisk):
                disks.append(self._disk_dict(dev))
            elif isinstance(dev, vim.vm.device.VirtualController) and hasattr(dev, "busNumber"):
                ctrls.append({"key": dev.key, "type": _cls(dev), "bus_number": dev.busNumber})
            elif isinstance(dev, vim.vm.device.VirtualEthernetCard):
                nics.append({"key": dev.key, "type": _cls(dev), "mac": dev.macAddress, "backing": dev.backing})
        return ctrls, disks, nics

    def _nic_network(self, vm: Any, backing: Any) -> str:
        from pyVmomi import vim
        if isinstance(backing, vim.vm.device.VirtualEthernetCard.NetworkBackingInfo):
            return backing.deviceName
        if isinstance(backing, vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo):
            key = backing.port.portgroupKey
            for n in vm.network:
                if isinstance(n, vim.dvs.DistributedVirtualPortgroup) and n.key == key:
                    return n.name
            return key
        if isinstance(backing, vim.vm.device.VirtualEthernetCard.OpaqueNetworkBackingInfo):
            return backing.opaqueNetworkId
        return ""

    def vm_info(self, moref: str) -> dict[str, Any]:
        vm = self._vm(moref)
        cfg = vm.config
        if cfg is None:
            raise ProviderError(f"VM {moref} has no config (inaccessible/orphaned?)")
        ctrls, disks, nics = self._devices(cfg.hardware)
        for n in nics:
            n["network"] = self._nic_network(vm, n.pop("backing"))
        boot = [b.deviceKey for b in (cfg.bootOptions.bootOrder or [])
                if _cls(b) == "BootableDiskDevice"] if cfg.bootOptions else []
        layout = vm.layoutEx
        return {"moref": moref, "name": cfg.name, "uuid": cfg.uuid, "num_cpu": cfg.hardware.numCPU,
                "cores_per_socket": cfg.hardware.numCoresPerSocket, "memory_mb": cfg.hardware.memoryMB,
                "firmware": cfg.firmware, "secure_boot": bool(getattr(cfg.bootOptions, "efiSecureBootEnabled", False)),
                "guest_id": cfg.guestId, "guest_full_name": cfg.guestFullName,
                "cbt_enabled": bool(cfg.changeTrackingEnabled), "boot_disk_keys": boot,
                "controllers": ctrls, "disks": disks, "nics": nics,
                "layout_disks": [{"key": d.key, "files": [f for c in d.chain for f in c.fileKey]}
                                 for d in (layout.disk if layout else [])],
                "datacenter": self.datacenter_of(vm)}

    def datacenter_of(self, vm: Any) -> str | None:
        from pyVmomi import vim
        p = vm.parent
        while p is not None and not isinstance(p, vim.Datacenter):
            p = getattr(p, "parent", None)
        return p.name if p is not None else None

    # ------------------------------------------------------------------ snapshots
    def create_snapshot(self, moref: str, name: str, *, quiesce: bool) -> str:
        if not name.startswith(SNAP_PREFIX):
            raise SafetyGateError("snapshot names must start with vmigrate-")
        vm = self._vm(moref)
        snap = self._wait(vm.CreateSnapshot_Task(name=name, description="vmigrate read point (safe to delete after "
                                                 "the migration/sync)", memory=False, quiesce=quiesce),
                          f"snapshot of {moref}" + (" (quiesced: needs VMware Tools)" if quiesce else ""))
        return snap._moId

    def _snapshot(self, snap_moref: str) -> Any:
        from pyVmomi import vim
        return vim.vm.Snapshot(snap_moref, self.si._stub)

    def snapshot_disks(self, moref: str, snap_moref: str) -> list[dict[str, Any]]:
        _, disks, _ = self._devices(self._snapshot(snap_moref).config.hardware)
        return disks

    def snapshot_name(self, moref: str, snap_moref: str) -> str | None:
        def walk(tree: Any) -> str | None:
            for n in tree or []:
                if n.snapshot._moId == snap_moref:
                    return n.name
                r = walk(n.childSnapshotList)
                if r:
                    return r
            return None
        vm = self._vm(moref)
        return walk(vm.snapshot.rootSnapshotList) if vm.snapshot else None

    def remove_snapshot(self, moref: str, snap_moref: str) -> None:
        name = self.snapshot_name(moref, snap_moref)
        if name is None:
            return  # already gone
        if not name.startswith(SNAP_PREFIX):
            raise SafetyGateError(f"refusing to remove snapshot {snap_moref} ({name!r}): not created by vmigrate")
        self._wait(self._snapshot(snap_moref).RemoveSnapshot_Task(removeChildren=False, consolidate=True),
                   f"remove snapshot {snap_moref}")

    # ------------------------------------------------------------------ CBT
    def query_changed(self, moref: str, snap_moref: str, device_key: int, start: int,
                      change_id: str) -> tuple[int, int, list[tuple[int, int]]]:
        try:
            r = self._vm(moref).QueryChangedDiskAreas(snapshot=self._snapshot(snap_moref), deviceKey=device_key,
                                                      startOffset=start, changeId=change_id)
        except Exception as e:  # noqa: BLE001 - CBT reset / invalid change id
            from ...core.errors import ChangeTrackingUnavailable
            raise ChangeTrackingUnavailable(f"QueryChangedDiskAreas({device_key}, {change_id}): "
                                            f"{getattr(e, 'msg', e)}") from e
        return r.startOffset, r.length, [(a.start, a.length) for a in (r.changedArea or [])]

    def transient(self, e: Exception) -> TransientProviderError:
        return TransientProviderError(str(e))
