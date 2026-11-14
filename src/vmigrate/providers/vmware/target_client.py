"""Purpose: the vSphere calls the VMware TARGET needs, behind one class, as plain dicts. The only target
module that imports pyVmomi; tests use a fake with the same methods.

Usage:
    c = VsphereTargetClient(host, user, password, datacenter="DC1", cluster="Prod", datastore="ds1")
    vm = c.create_vm("vmigrate-holder-abc", controller="lsilogic-sas", extra={"vmigrate.managed": "true"})
    d = c.add_disk(vm, size_bytes)             # {uuid, key, file, capacity, bus, unit}
    c.vm_info(vm); c.reconfigure(vm, {...}); c.power_on(vm); c.destroy(vm)

Not run against a real vCenter yet (unit tests cover the target logic with a fake client). Every object
is created inside the configured datacenter / cluster (or host) / resource pool / folder / datastore.
"""

from __future__ import annotations

from typing import Any

from ...core.errors import ConfigError, ProviderError
from .client import server_thumbprint

UNITS = [u for u in range(16) if u != 7]          # SCSI unit 7 is the controller itself
GUEST_IDS = {"windows": "windows9Server64Guest", "ubuntu": "ubuntu64Guest", "debian": "debian11_64Guest",
             "rhel": "rhel9_64Guest", "centos": "centos8_64Guest", "rocky": "rockylinux_64Guest",
             "almalinux": "almalinux_64Guest", "ol": "oracleLinux9_64Guest", "sles": "sles15_64Guest",
             "fedora": "fedora64Guest", "linux": "otherLinux64Guest"}


def slot(index: int) -> tuple[int, int]:
    """Disk index -> (SCSI bus, unit): 15 disks per controller, up to 4 controllers."""
    if index >= 4 * len(UNITS):
        raise ProviderError(f"vmware: {index + 1} disks exceed 4 SCSI controllers x 15 disks")
    return index // len(UNITS), UNITS[index % len(UNITS)]


class VsphereTargetClient:
    def __init__(self, host: str, user: str, password: str, *, port: int = 443, verify_tls: bool = True,
                 datacenter: str | None = None, cluster: str | None = None, esx_host: str | None = None,
                 resource_pool: str | None = None, folder: str | None = None, datastore: str | None = None):
        from pyVim.connect import SmartConnect
        try:
            self.si = SmartConnect(host=host, user=user, pwd=password, port=port, disableSslCertValidation=not verify_tls)
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"cannot connect to vSphere {host}: {e}") from e
        self.host, self.port = host, port
        self.content = self.si.RetrieveContent()
        self.cfg = {"datacenter": datacenter, "cluster": cluster, "host": esx_host, "resource_pool": resource_pool,
                    "folder": folder, "datastore": datastore}

    def close(self) -> None:
        from pyVim.connect import Disconnect
        try:
            Disconnect(self.si)
        except Exception:  # noqa: BLE001
            pass

    def thumbprint(self) -> str:
        return server_thumbprint(self.host, self.port)

    # ------------------------------------------------------------------ lookups
    def _all(self, vimtype: Any, root: Any = None) -> list[Any]:
        view = self.content.viewManager.CreateContainerView(root or self.content.rootFolder, [vimtype], True)
        try:
            return list(view.view)
        finally:
            view.Destroy()

    def _one(self, vimtype: Any, name: str | None, what: str, root: Any = None) -> Any:
        objs = self._all(vimtype, root)
        if name:
            objs = [o for o in objs if o.name == name]
        if len(objs) != 1:
            raise ConfigError(f"vmware target: {what} {name or '(not set)'!r}: found {len(objs)}"
                              + ("; set it on the endpoint" if not name else ""))
        return objs[0]

    def datacenter(self) -> Any:
        from pyVmomi import vim
        return self._one(vim.Datacenter, self.cfg["datacenter"], "datacenter")

    def _compute(self) -> Any:
        from pyVmomi import vim
        dc = self.datacenter()
        if self.cfg["cluster"]:
            return self._one(vim.ClusterComputeResource, self.cfg["cluster"], "cluster", dc.hostFolder)
        if self.cfg["host"]:
            h = self._one(vim.HostSystem, self.cfg["host"], "host", dc.hostFolder)
            return h.parent
        return self._one(vim.ComputeResource, None, "cluster or host", dc.hostFolder)

    def _pool(self) -> Any:
        from pyVmomi import vim
        comp = self._compute()
        if self.cfg["resource_pool"]:
            return self._one(vim.ResourcePool, self.cfg["resource_pool"], "resource pool", comp)
        return comp.resourcePool

    def _folder(self) -> Any:
        from pyVmomi import vim
        dc = self.datacenter()
        if self.cfg["folder"]:
            return self._one(vim.Folder, self.cfg["folder"], "VM folder", dc.vmFolder)
        return dc.vmFolder

    def _datastore(self) -> Any:
        from pyVmomi import vim
        return self._one(vim.Datastore, self.cfg["datastore"], "datastore", self.datacenter())

    def _vm(self, moref: str) -> Any:
        from pyVmomi import vim
        return vim.VirtualMachine(moref, self.si._stub)

    def _wait(self, task: Any, what: str) -> Any:
        from pyVim.task import WaitForTask
        try:
            WaitForTask(task)
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"{what} failed: {getattr(e, 'msg', None) or e}") from e
        return task.info.result

    def _network(self, name: str) -> Any:
        hits = [n for n in self.datacenter().network if n.name == name]
        if len(hits) != 1:
            raise ConfigError(f"vmware target: network {name!r}: found {len(hits)}")
        return hits[0]

    # ------------------------------------------------------------------ inventory for the wizard / checks
    def about(self) -> dict[str, str]:
        a = self.content.about
        return {"name": a.fullName, "api": a.apiVersion, "type": a.apiType}

    def check(self) -> list[str]:
        p = []
        for what, fn in (("datacenter", self.datacenter), ("cluster/host", self._compute), ("resource pool", self._pool),
                         ("folder", self._folder), ("datastore", self._datastore)):
            try:
                fn()
            except ConfigError as e:
                p.append(str(e))
            except Exception as e:  # noqa: BLE001
                p.append(f"{what}: {e}")
        return p

    def networks(self) -> list[dict[str, Any]]:
        return [{"name": n.name, "type": type(n).__name__.split(".")[-1]} for n in self.datacenter().network]

    def datastores(self) -> list[dict[str, Any]]:
        return [{"name": d.name, "free": int(d.summary.freeSpace or 0), "accessible": bool(d.summary.accessible)}
                for d in self.datacenter().datastore]

    # ------------------------------------------------------------------ VMs + disks
    def _controller(self, kind: str, bus: int, key: int) -> Any:
        from pyVmomi import vim
        cls = {"lsilogic-sas": vim.vm.device.VirtualLsiLogicSASController,
               "pvscsi": vim.vm.device.ParaVirtualSCSIController,
               "lsilogic": vim.vm.device.VirtualLsiLogicController}.get(kind)
        if cls is None:
            raise ConfigError(f"vmware target: scsi_controller must be lsilogic-sas, pvscsi or lsilogic (got {kind!r})")
        c = cls()
        c.key, c.busNumber, c.sharedBus = key, bus, vim.vm.device.VirtualSCSIController.Sharing.noSharing
        return vim.vm.device.VirtualDeviceSpec(operation=vim.vm.device.VirtualDeviceSpec.Operation.add, device=c)

    def create_vm(self, name: str, *, controller: str, annotation: str, extra: dict[str, str]) -> str:
        from pyVmomi import vim
        ds = self._datastore()
        spec = vim.vm.ConfigSpec(name=name, memoryMB=512, numCPUs=1, guestId="otherGuest64", annotation=annotation,
                                 files=vim.vm.FileInfo(vmPathName=f"[{ds.name}]"),
                                 deviceChange=[self._controller(controller, 0, -100)],
                                 extraConfig=[vim.option.OptionValue(key=k, value=v) for k, v in extra.items()])
        vm = self._wait(self._folder().CreateVM_Task(config=spec, pool=self._pool()), f"create VM {name}")
        return str(vm._moId)

    def vm_info(self, moref: str) -> dict[str, Any] | None:
        from pyVmomi import vim, vmodl
        vm = self._vm(moref)
        try:
            cfg = vm.config
        except vmodl.fault.ManagedObjectNotFound:
            return None
        if cfg is None:
            return None
        ctl = {d.key: d for d in cfg.hardware.device if isinstance(d, vim.vm.device.VirtualSCSIController)}
        disks = []
        for d in cfg.hardware.device:
            if isinstance(d, vim.vm.device.VirtualDisk):
                c = ctl.get(d.controllerKey)
                disks.append({"uuid": d.backing.uuid, "key": d.key, "file": d.backing.fileName,
                              "capacity": int(d.capacityInBytes or d.capacityInKB * 1024),
                              "bus": c.busNumber if c else 99, "unit": d.unitNumber})
        return {"moref": moref, "name": vm.name, "power": str(vm.runtime.powerState),
                "tools": str(getattr(vm.guest, "toolsRunningStatus", "") or ""),
                "heartbeat": str(getattr(vm, "guestHeartbeatStatus", "") or ""),
                "annotation": cfg.annotation or "", "extra": {o.key: str(o.value) for o in cfg.extraConfig or []},
                "vmx_dir": cfg.files.vmPathName.rsplit("/", 1)[0] + "/",
                "controllers": {c.busNumber: (c.key, type(c).__name__.split(".")[-1]) for c in ctl.values()},
                "disks": sorted(disks, key=lambda x: (x["bus"], x["unit"]))}

    def managed_vms(self) -> list[dict[str, Any]]:
        from pyVmomi import vim
        out = []
        for vm in self._all(vim.VirtualMachine, self.datacenter().vmFolder):
            try:
                extra = {o.key: str(o.value) for o in (vm.config.extraConfig if vm.config else []) or []}
            except Exception:  # noqa: BLE001 - VM vanished while listing
                continue
            if extra.get("vmigrate.managed") == "true":
                out.append({"moref": vm._moId, "name": vm.name, "extra": extra})
        return out

    def find_vms(self, name: str) -> list[dict[str, Any]]:
        from pyVmomi import vim
        return [{"moref": vm._moId, "name": vm.name} for vm in self._all(vim.VirtualMachine, self.datacenter().vmFolder)
                if vm.name == name]

    def add_disk(self, moref: str, size_bytes: int, index: int, *, controller: str,
                 existing_file: str | None = None) -> dict[str, Any]:
        """Add a thin disk (or attach `existing_file`) at slot(index); adds the SCSI controller for that bus."""
        from pyVmomi import vim
        info = self.vm_info(moref) or {}
        bus, unit = slot(index)
        changes = []
        ctl = (info.get("controllers") or {}).get(bus)
        ckey = ctl[0] if ctl else -200 - bus
        if not ctl:
            changes.append(self._controller(controller, bus, ckey))
        d = vim.vm.device.VirtualDisk()
        d.key, d.controllerKey, d.unitNumber = -300, ckey, unit
        b = vim.vm.device.VirtualDisk.FlatVer2BackingInfo(diskMode="persistent", thinProvisioned=True)
        b.fileName = existing_file or ""
        d.backing = b
        spec = vim.vm.device.VirtualDeviceSpec(operation=vim.vm.device.VirtualDeviceSpec.Operation.add, device=d)
        if not existing_file:
            d.capacityInBytes, d.capacityInKB = size_bytes, size_bytes // 1024
            spec.fileOperation = vim.vm.device.VirtualDeviceSpec.FileOperation.create
        changes.append(spec)
        self._wait(self._vm(moref).ReconfigVM_Task(spec=vim.vm.ConfigSpec(deviceChange=changes)), f"add disk to {moref}")
        new = [x for x in (self.vm_info(moref) or {}).get("disks", []) if x["bus"] == bus and x["unit"] == unit]
        if not new:
            raise ProviderError(f"vmware: disk at SCSI {bus}:{unit} not found after adding it")
        return new[0]

    def set_extra(self, moref: str, extra: dict[str, str]) -> None:
        from pyVmomi import vim
        spec = vim.vm.ConfigSpec(extraConfig=[vim.option.OptionValue(key=k, value=v) for k, v in extra.items()])
        self._wait(self._vm(moref).ReconfigVM_Task(spec=spec), f"set extraConfig on {moref}")

    def remove_disk(self, moref: str, key: int, *, destroy: bool) -> None:
        from pyVmomi import vim
        vm = self._vm(moref)
        dev = next((d for d in vm.config.hardware.device if d.key == key), None)
        if dev is None:
            return
        spec = vim.vm.device.VirtualDeviceSpec(operation=vim.vm.device.VirtualDeviceSpec.Operation.remove, device=dev)
        if destroy:
            spec.fileOperation = vim.vm.device.VirtualDeviceSpec.FileOperation.destroy
        self._wait(vm.ReconfigVM_Task(spec=vim.vm.ConfigSpec(deviceChange=[spec])), f"remove disk {key} from {moref}")

    def copy_disk(self, src_file: str, dst_file: str) -> None:
        from pyVmomi import vim
        dc = self.datacenter()
        spec = vim.VirtualDiskManager.FileBackedVirtualDiskSpec(diskType="thin", adapterType="lsiLogic")
        self._wait(self.content.virtualDiskManager.CopyVirtualDisk_Task(
            sourceName=src_file, sourceDatacenter=dc, destName=dst_file, destDatacenter=dc, destSpec=spec, force=False),
            f"copy disk {src_file}")

    def reconfigure(self, moref: str, c: dict[str, Any]) -> None:
        """Turn the holder into the migrated VM. c: {name, num_cpu, cores_per_socket, memory_mb, guest_id,
        firmware, secure_boot, boot_disk_key, annotation, extra, nics: [{network, adapter, mac}]}."""
        from pyVmomi import vim
        spec = vim.vm.ConfigSpec(name=c["name"], numCPUs=c["num_cpu"], numCoresPerSocket=c["cores_per_socket"],
                                 memoryMB=c["memory_mb"], guestId=c["guest_id"], annotation=c["annotation"],
                                 firmware=c["firmware"],
                                 extraConfig=[vim.option.OptionValue(key=k, value=v) for k, v in c["extra"].items()])
        boot = vim.vm.BootOptions(bootOrder=[vim.vm.BootOptions.BootableDiskDevice(deviceKey=c["boot_disk_key"])])
        if c["firmware"] == "efi":
            boot.efiSecureBootEnabled = bool(c["secure_boot"])
        spec.bootOptions = boot
        changes = []
        for i, n in enumerate(c["nics"]):
            cls = {"vmxnet3": vim.vm.device.VirtualVmxnet3, "e1000e": vim.vm.device.VirtualE1000e}[n["adapter"]]
            nic = cls()
            nic.key = -400 - i
            net = self._network(n["network"])
            if isinstance(net, vim.dvs.DistributedVirtualPortgroup):
                nic.backing = vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo(
                    port=vim.dvs.PortConnection(portgroupKey=net.key, switchUuid=net.config.distributedVirtualSwitch.uuid))
            else:
                nic.backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(deviceName=net.name, network=net)
            nic.connectable = vim.vm.device.VirtualDevice.ConnectInfo(startConnected=True, allowGuestControl=True,
                                                                      connected=False)
            if n.get("mac"):
                nic.addressType, nic.macAddress = "manual", n["mac"]
            changes.append(vim.vm.device.VirtualDeviceSpec(operation=vim.vm.device.VirtualDeviceSpec.Operation.add,
                                                           device=nic))
        spec.deviceChange = changes
        self._wait(self._vm(moref).ReconfigVM_Task(spec=spec), f"configure VM {c['name']}")

    def power_on(self, moref: str) -> None:
        self._wait(self._vm(moref).PowerOnVM_Task(), f"power on {moref}")

    def power_off(self, moref: str) -> None:
        vm = self._vm(moref)
        if str(vm.runtime.powerState) == "poweredOn":
            self._wait(vm.PowerOffVM_Task(), f"power off {moref}")

    def destroy(self, moref: str) -> None:
        self._wait(self._vm(moref).Destroy_Task(), f"delete VM {moref}")
