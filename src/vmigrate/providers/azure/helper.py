"""Purpose: the temporary Linux helper VM of the Azure target's `helper_vm` write mode (the approach Coriolis
documents for Azure as a destination: a "disk copy worker" in Azure with the target disks attached). Managed disks
are attached to it as data disks and written / read / OSMorphed over SSH through the same NBD agent as on VHI.

Lifecycle (one helper per run; created on first use, removed by destroy()):
  1. throwaway SSH client key + SSH host key generated here; the host key goes in with cloud-init (customData)
     and is pinned, so the first SSH connection is authenticated (no trust on first use)
  2. a NIC in `helper_subnet` (+ a Standard public IP when helper_public_ip: true; the NIC's private IP otherwise:
     the worker must then reach the subnet over VPN / ExpressRoute / peering), optional `helper_nsg_id`
  3. the VM: `helper_image` (default Ubuntu 24.04 LTS), `helper_size` (default Standard_D4s_v5: 8 data disks;
     use a bigger size for VMs with more disks), OS disk + NIC deleted with the VM
  4. attach(disk) -> data disk at the lowest free LUN -> /dev/disk/azure/scsi1/lun<N> inside the helper
Every resource is tagged vmigrate:managed + vmigrate:helper and named vmigrate-helper-<run>.

Usage:
    h = AzureHelper(client, cfg, ctx, workdir, rg_path=..., location=...)
    t = h.ensure(); dev = h.attach(disk_id); h.detach(disk_id); h.destroy()

VERIFY ON FIRST LIVE RUN: the LUN device path comes from the Azure udev rules shipped in Azure's Ubuntu images
(azure-vm-utils / walinuxagent); images without them need helper_device_glob. Tested against a fake ARM only.
"""

from __future__ import annotations

import base64
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ...core import cancel
from ...core.context import RunContext
from ...core.errors import ConfigError, ProviderError, SafetyGateError
from ..vhi.helper import HOST_ALIAS, SshTransport, cloud_config, ssh_keygen
from .client import COMPUTE_API, NETWORK_API

MANAGED, HELPER = "vmigrate:managed", "vmigrate:helper"
DEFAULT_IMAGE = {"publisher": "Canonical", "offer": "ubuntu-24_04-lts", "sku": "server", "version": "latest"}
ADMIN = "azureadm"


class AzureHelper:
    def __init__(self, client: Any, cfg: Mapping[str, Any], ctx: RunContext, workdir: Path, *, rg_path: str,
                 location: str, transport_factory: Callable[..., Any] = SshTransport,
                 keygen: Callable[[Path], str] = ssh_keygen, poll: float = 5):
        self.c, self.cfg, self.ctx = client, cfg, ctx
        self.rg, self.location = rg_path, location
        self.name = f"vmigrate-helper-{(ctx.run_id or 'run')[:12]}"
        self.dir = Path(workdir) / self.name
        self._tf, self._keygen, self.poll = transport_factory, keygen, poll
        self.transport: Any = None
        self.host: str | None = None
        self.attached: dict[str, tuple[int, str]] = {}          # disk id (lower) -> (lun, device)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ paths
    @property
    def vm_path(self) -> str:
        return f"{self.rg}/providers/Microsoft.Compute/virtualMachines/{self.name}"

    @property
    def nic_path(self) -> str:
        return f"{self.rg}/providers/Microsoft.Network/networkInterfaces/{self.name}-nic"

    @property
    def pip_path(self) -> str:
        return f"{self.rg}/providers/Microsoft.Network/publicIPAddresses/{self.name}-ip"

    def _put(self, path: str, body: dict[str, Any], api: str, what: str) -> dict[str, Any]:
        st, h, d = self.c.request("PUT", path, api=api, body=body, ok=(200, 201, 202))
        if st in (201, 202) and (h.get("azure-asyncoperation") or h.get("location")):
            self.c.wait_async(h, what)
            return self.c.get(path, api)
        return d

    def _delete(self, path: str, api: str, what: str) -> None:
        st, h, _ = self.c.request("DELETE", path, api=api, ok=(200, 202, 204, 404))
        if st == 202:
            self.c.wait_async(h, what)

    def problems(self) -> list[str]:
        p: list[str] = []
        if not self.cfg.get("helper_subnet"):
            p.append("helper_vm write mode: set helper_subnet (subnet resource ID for the helper VM; the worker must "
                     "reach it over SSH, or set helper_public_ip: true)")
        elif self.c.get_or_none(self.cfg["helper_subnet"], NETWORK_API) is None:
            p.append(f"helper subnet {self.cfg['helper_subnet']} not found")
        if self.cfg.get("helper_nsg_id") and self.c.get_or_none(self.cfg["helper_nsg_id"], NETWORK_API) is None:
            p.append(f"helper network security group {self.cfg['helper_nsg_id']} not found")
        for tool in ("ssh", "ssh-keygen"):
            if shutil.which(tool) is None:
                p.append(f"{tool} not found on this worker (install openssh-client)")
        return p

    # ------------------------------------------------------------------ lifecycle
    def ensure(self) -> Any:
        with self._lock:
            if self.transport is not None:
                return self.transport
            return self._create()

    def _tags(self) -> dict[str, str]:
        return {MANAGED: "true", HELPER: "true", "vmigrate:run": (self.ctx.run_id or "")[:256]}

    def _create(self) -> Any:
        bad = self.problems()
        if bad:
            raise ConfigError("; ".join(bad))
        self.ctx.require_execute("azure create helper VM", name=self.name, size=self.cfg.get("helper_size"))
        self._remove_leftover()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.dir.chmod(0o700)
        client_pub = self._keygen(self.dir / "id_ed25519")
        host_pub = self._keygen(self.dir / "host_ed25519")
        known = self.dir / "known_hosts"
        known.write_text(f"{HOST_ALIAS} {' '.join(host_pub.split()[:2])}\n")
        known.chmod(0o600)
        ipconf: dict[str, Any] = {"subnet": {"id": self.cfg["helper_subnet"]}, "privateIPAllocationMethod": "Dynamic",
                                  "primary": True}
        if self.cfg.get("helper_public_ip"):
            pip = self._put(self.pip_path, {"location": self.location, "tags": self._tags(), "sku": {"name": "Standard"},
                                            "properties": {"publicIPAllocationMethod": "Static"}},
                            NETWORK_API, "create helper public IP")
            ipconf["publicIPAddress"] = {"id": pip.get("id") or self.pip_path}
        nic_body: dict[str, Any] = {"location": self.location, "tags": self._tags(),
                                    "properties": {"ipConfigurations": [{"name": "ipconfig1", "properties": ipconf}]}}
        if self.cfg.get("helper_nsg_id"):
            nic_body["properties"]["networkSecurityGroup"] = {"id": self.cfg["helper_nsg_id"]}
        nic = self._put(self.nic_path, nic_body, NETWORK_API, "create helper NIC")
        ud = base64.b64encode(cloud_config(client_pub, (self.dir / "host_ed25519").read_text(), host_pub).encode()).decode()
        image = self.cfg.get("helper_image") or DEFAULT_IMAGE
        image_ref = {"id": image} if isinstance(image, str) else dict(image)
        body = {"location": self.location, "tags": self._tags(), "properties": {
            "hardwareProfile": {"vmSize": self.cfg.get("helper_size") or "Standard_D4s_v5"},
            "storageProfile": {"imageReference": image_ref,
                               "osDisk": {"createOption": "FromImage", "deleteOption": "Delete",
                                          "managedDisk": {"storageAccountType": "StandardSSD_LRS"}},
                               "dataDisks": []},
            "osProfile": {"computerName": self.name[:64], "adminUsername": ADMIN, "customData": ud,
                          "linuxConfiguration": {"disablePasswordAuthentication": True, "ssh": {"publicKeys": [
                              {"path": f"/home/{ADMIN}/.ssh/authorized_keys", "keyData": client_pub}]}}},
            "networkProfile": {"networkInterfaces": [{"id": nic.get("id") or self.nic_path,
                                                      "properties": {"primary": True, "deleteOption": "Delete"}}]}}}
        self.ctx.logger.info("helper VM %s: booting %s in %s", self.name, image_ref, self.cfg["helper_subnet"])
        self._put(self.vm_path, body, COMPUTE_API, f"create helper VM {self.name}")
        host = self._address()
        t = self._tf(host, self.dir / "id_ed25519", known, port=22)
        self._wait_ssh(t, host)
        if not t.run("command -v python3 || true").strip():
            raise ProviderError("the helper image has no python3; use a standard Ubuntu / RHEL-family image")
        self.transport, self.host = t, host
        self.ctx.logger.info("helper VM %s ready at %s", self.name, host)
        return t

    def _address(self) -> str:
        if self.cfg.get("helper_public_ip"):
            ip = ((self.c.get(self.pip_path, NETWORK_API).get("properties") or {}).get("ipAddress"))
        else:
            cfgs = (self.c.get(self.nic_path, NETWORK_API).get("properties") or {}).get("ipConfigurations") or [{}]
            ip = (cfgs[0].get("properties") or {}).get("privateIPAddress")
        if not ip:
            raise ProviderError(f"helper VM {self.name} has no IP address yet")
        return str(ip)

    def _wait_ssh(self, t: Any, host: str) -> None:
        deadline = time.monotonic() + float(self.cfg.get("helper_boot_timeout", 900))
        last = ""
        while time.monotonic() < deadline:
            try:
                t.run("true", timeout=30)
                return
            except (ProviderError, subprocess.TimeoutExpired) as e:
                last = str(e)
                cancel.sleep(self.poll)
        raise ProviderError(f"helper VM {self.name}: no SSH to {host}:22 within the boot timeout. The worker must reach "
                            f"the helper subnet (VPN / ExpressRoute / peering) or use helper_public_ip: true, and the NSG "
                            f"must allow TCP 22 from this worker. Last error: {last[:400]}")

    def _remove_leftover(self) -> None:
        old = self.c.get_or_none(self.vm_path, COMPUTE_API)
        if old is None:
            return
        if (old.get("tags") or {}).get(HELPER) != "true":
            raise SafetyGateError(f"a VM named {self.name!r} exists and is not a vmigrate helper")
        self.ctx.logger.warning("deleting leftover helper VM %s from an interrupted run", self.name)
        self._delete_vm()

    def _delete_vm(self) -> None:
        self._delete(self.vm_path, COMPUTE_API, "delete helper VM")       # OS disk + NIC go with it (deleteOption)
        for path, api in ((self.nic_path, NETWORK_API), (self.pip_path, NETWORK_API)):
            r = self.c.get_or_none(path, api)
            if r is not None and (r.get("tags") or {}).get(HELPER) == "true":
                self._delete(path, api, "delete helper network resource")

    # ------------------------------------------------------------------ data disks
    def _vm(self) -> dict[str, Any]:
        return self.c.get(self.vm_path, COMPUTE_API)

    def _set_data_disks(self, disks: list[dict[str, Any]], what: str) -> None:
        vm = self._vm()
        props = dict(vm.get("properties") or {})
        sp = dict(props.get("storageProfile") or {})
        sp["dataDisks"] = disks
        body = {"location": vm.get("location", self.location), "tags": vm.get("tags") or self._tags(),
                "properties": {**props, "storageProfile": sp}}
        for k in ("provisioningState", "vmId", "instanceView", "timeCreated"):
            body["properties"].pop(k, None)
        self._put(self.vm_path, body, COMPUTE_API, what)

    def attach(self, disk_id: str) -> str:
        with self._lock:
            key = disk_id.lower()
            if key in self.attached:
                return self.attached[key][1]
            t = self.ensure()
            d = self.c.get_or_none(disk_id, "2024-03-02")
            if d is None or (d.get("tags") or {}).get(MANAGED) != "true":
                raise SafetyGateError(f"refusing to attach {disk_id} to the helper: not vmigrate-managed")
            self.ctx.require_execute("attach disk to helper VM", disk=disk_id.rsplit("/", 1)[-1], helper=self.name)
            current = list((self._vm().get("properties") or {}).get("storageProfile", {}).get("dataDisks") or [])
            used = {int(x["lun"]) for x in current}
            lun = next(i for i in range(64) if i not in used)
            current.append({"lun": lun, "createOption": "Attach", "managedDisk": {"id": disk_id}})
            self._set_data_disks(current, f"attach disk to helper (LUN {lun})")
            dev = self._device(t, lun)
            self.attached[key] = (lun, dev)
            self.ctx.logger.info("disk %s attached to helper as %s (LUN %d)", disk_id.rsplit("/", 1)[-1], dev, lun)
            return dev

    def _device(self, t: Any, lun: int) -> str:
        glob = str(self.cfg.get("helper_device_glob") or "/dev/disk/azure/scsi1/lun{lun}").format(lun=lun)
        deadline = time.monotonic() + float(self.cfg.get("helper_device_timeout", 180))
        while time.monotonic() < deadline:
            out = t.run(f"readlink -f {shlex.quote(glob)} 2>/dev/null || true").strip()
            if out.startswith("/dev/") and out != glob:
                return out
            cancel.sleep(max(1.0, self.poll / 2))
        raise ProviderError(f"helper VM: no device for LUN {lun} at {glob} (image without the Azure udev rules? "
                            "set helper_device_glob)")

    def detach(self, disk_id: str) -> None:
        with self._lock:
            key = disk_id.lower()
            if key not in self.attached:
                return
            lun, dev = self.attached[key]
            if self.transport is not None:
                try:
                    self.transport.run(f"sync; sudo -n blockdev --flushbufs {shlex.quote(dev)} || true")
                except ProviderError:
                    pass
            current = [x for x in (self._vm().get("properties") or {}).get("storageProfile", {}).get("dataDisks") or []
                       if int(x["lun"]) != lun]
            self._set_data_disks(current, f"detach LUN {lun} from helper")
            del self.attached[key]

    def detach_all(self) -> None:
        for key in list(self.attached):
            self.detach(key)

    def destroy(self) -> None:
        with self._lock:
            if self.transport is None and not self.dir.exists() and self.c.get_or_none(self.vm_path, COMPUTE_API) is None:
                return
            vm = self.c.get_or_none(self.vm_path, COMPUTE_API)
            if vm is not None:
                if (vm.get("tags") or {}).get(HELPER) != "true":
                    raise SafetyGateError(f"{self.vm_path} is not a vmigrate helper; refusing to delete it")
                self._delete_vm()                 # its data disks are detached by the delete and left intact
            self.attached.clear()
            self.transport = self.host = None
            shutil.rmtree(self.dir, ignore_errors=True)
            self.ctx.logger.info("helper VM %s removed", self.name)
