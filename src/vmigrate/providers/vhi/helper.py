"""Purpose: the temporary helper VM for the VHI target's `helper_vm` write mode, the Coriolis-style
path that needs only API access to VHI (no vStorage mount, worker can run anywhere that reaches the
helper over SSH).

Lifecycle (one helper per run, created on first use, destroyed by close()/cleanup):
  1. generate a throwaway SSH client key AND an SSH host key locally (ssh-keygen); the host key is
     injected with cloud-init and pinned, so the first connection is authenticated (no TOFU)
  2. security groups: the project's `default` group unless `helper_security_groups` names others (the
     Coriolis behaviour); a temporary group allowing TCP/22 from `helper_ssh_cidr` only when
     `helper_create_security_group: true`
  3. boot `helper_image` from a small volume (helper_root_gib) on `helper_network`, optional floating
     IP from `helper_floating_network`; cloud-init creates user `vmigrate` with passwordless sudo
  4. wait for SSH and python3
  5. attach(volume) -> /dev/disk/by-id path inside the helper; detach(volume); destroy()
Every object it creates is tagged vmigrate:managed / vmigrate:helper and named vmigrate-helper-<run>.

Usage:
    h = HelperVm(api, cfg, ctx, workdir); t = h.ensure(); dev = h.attach(volume_id); h.destroy()
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

from ...core.context import RunContext
from ...core.errors import ConfigError, ProviderError, SafetyGateError
from ...core.retry import poll_until

MANAGED = "vmigrate:managed"
HELPER = "vmigrate:helper"
HOST_ALIAS = "vmigrate-helper"
USER = "vmigrate"


class SshTransport:
    """Runs commands on the helper over OpenSSH with a pinned host key."""

    def __init__(self, host: str, key: Path, known_hosts: Path, *, user: str = USER, port: int = 22,
                 connect_timeout: int = 15, ciphers: str | None = None):
        self.host, self.key, self.known_hosts, self.user, self.port = host, key, known_hosts, user, port
        self.connect_timeout = connect_timeout
        self.ciphers = ciphers or "aes128-gcm@openssh.com,aes256-gcm@openssh.com,chacha20-poly1305@openssh.com"

    def argv(self, cmd: str) -> list[str]:
        return ["ssh", "-i", str(self.key), "-p", str(self.port),
                "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                "-o", f"HostKeyAlias={HOST_ALIAS}", "-o", "HostKeyAlgorithms=ssh-ed25519",
                "-o", f"UserKnownHostsFile={self.known_hosts}", "-o", "StrictHostKeyChecking=yes",
                "-o", f"ConnectTimeout={self.connect_timeout}", "-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=8", "-o", "Compression=no", "-o", f"Ciphers={self.ciphers}",
                f"{self.user}@{self.host}", cmd]

    def run(self, cmd: str, timeout: float = 120) -> str:
        p = subprocess.run(self.argv(cmd), capture_output=True, timeout=timeout)
        if p.returncode != 0:
            raise ProviderError(f"helper ssh `{cmd[:80]}` failed ({p.returncode}): "
                                f"{p.stderr.decode(errors='replace').strip()[:600]}")
        return p.stdout.decode(errors="replace")

    def popen(self, cmd: str) -> subprocess.Popen:
        return subprocess.Popen(self.argv(cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0)


def ssh_keygen(path: Path) -> str:
    """ed25519 key pair at path / path.pub; returns the public key line."""
    path.unlink(missing_ok=True)
    Path(f"{path}.pub").unlink(missing_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "vmigrate-helper", "-f", str(path)],
                   check=True, capture_output=True)
    path.chmod(0o600)
    return Path(f"{path}.pub").read_text().strip()


def cloud_config(client_pub: str, host_priv: str, host_pub: str) -> str:
    priv = "\n".join("    " + ln for ln in host_priv.strip().splitlines())
    return f"""#cloud-config
users:
  - default
  - name: {USER}
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys:
      - {client_pub}
ssh_deletekeys: true
ssh_genkeytypes: []
ssh_keys:
  ed25519_private: |
{priv}
  ed25519_public: {host_pub}
"""


def find_device(listing: str, lsblk: str, volume_id: str) -> str | None:
    """Locate a Cinder volume inside the guest: virtio serial = first 20 chars of the volume ID."""
    serial = volume_id[:20]
    names = [n.strip() for n in listing.splitlines() if n.strip() and "-part" not in n]
    for n in names:
        if n == f"virtio-{serial}" or n.startswith(f"virtio-{serial}"):
            return f"/dev/disk/by-id/{n}"
    for n in names:
        if volume_id in n or volume_id.replace("-", "") in n:
            return f"/dev/disk/by-id/{n}"
    for line in lsblk.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] in (serial, volume_id):
            return f"/dev/{parts[0]}"
    return None


class HelperVm:
    def __init__(self, api: Any, cfg: Mapping[str, Any], ctx: RunContext, workdir: Path, *,
                 transport_factory: Callable[..., Any] = SshTransport,
                 keygen: Callable[[Path], str] = ssh_keygen, poll: float = 5, op_timeout: float = 1800):
        self.api, self.cfg, self.ctx = api, cfg, ctx
        self.name = f"vmigrate-helper-{(ctx.run_id or 'run')[:12]}"
        self.dir = Path(workdir) / self.name
        self._tf, self._keygen = transport_factory, keygen
        self.poll, self.op_timeout = poll, op_timeout
        self.server: dict[str, Any] | None = None
        self.transport: Any = None
        self.sg_id: str | None = None
        self.fip_id: str | None = None
        self.attached: dict[str, str] = {}          # volume id -> device path in the helper
        # disks are copied in parallel: one helper per run, and attach/detach one at a time
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ checks (read-only)
    def problems(self) -> list[str]:
        p: list[str] = []
        img, net = self.cfg.get("helper_image"), self.cfg.get("helper_network")
        if not img:
            p.append("helper_vm write mode: set helper_image (a Linux cloud image with python3 and cloud-init, "
                     "e.g. Ubuntu 22.04/24.04 or Rocky 9)")
        elif not self.api.find_image(img):
            p.append(f"helper image {img!r} not found")
        if not net:
            p.append("helper_vm write mode: set helper_network (the network the helper VM is attached to)")
        elif not self.api.find_network(net):
            p.append(f"helper network {net!r} not found")
        fnet = self.cfg.get("helper_floating_network")
        if fnet and not self.api.find_network(fnet):
            p.append(f"helper floating-IP network {fnet!r} not found")
        fl = self.cfg.get("helper_flavor")
        if fl and not self.api.find_flavor(fl):
            p.append(f"helper flavor {fl!r} not found")
        if not self.cfg.get("helper_create_security_group"):
            for g in self.security_groups():
                if not self.api.find_security_group(g):
                    p.append(f"helper security group {g!r} not found in the project")
        for tool in ("ssh", "ssh-keygen"):
            if shutil.which(tool) is None:
                p.append(f"{tool} not found on this worker (install openssh-client)")
        return p

    def security_groups(self) -> list[str]:
        g = self.cfg.get("helper_security_groups") or "default"
        names = [x.strip() for x in (g.split(",") if isinstance(g, str) else g) if str(x).strip()]
        return names or ["default"]

    # ------------------------------------------------------------------ lifecycle
    def ensure(self) -> Any:
        with self._lock:
            if self.transport is not None:
                return self.transport
            return self._create()

    def _create(self) -> Any:
        bad = self.problems()
        if bad:
            raise ConfigError("; ".join(bad))
        self.ctx.require_execute("create helper VM", name=self.name, image=self.cfg.get("helper_image"))
        self._delete_leftover()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.dir.chmod(0o700)
        client_pub = self._keygen(self.dir / "id_ed25519")
        host_pub = self._keygen(self.dir / "host_ed25519")
        host_priv = (self.dir / "host_ed25519").read_text()
        known = self.dir / "known_hosts"
        known.write_text(f"{HOST_ALIAS} {' '.join(host_pub.split()[:2])}\n")
        known.chmod(0o600)

        if self.cfg.get("helper_create_security_group"):
            sg = self.api.create_ssh_security_group(f"{self.name}-ssh", self.cfg.get("helper_ssh_cidr", "0.0.0.0/0"))
            self.sg_id = sg["id"]
            sgs = [{"name": sg["name"]}]
        else:                                      # the project's own groups, nothing created
            sgs = [{"name": g} for g in self.security_groups()]
        img = self.api.find_image(self.cfg["helper_image"])
        net = self.api.find_network(self.cfg["helper_network"])
        flavor = self._flavor()
        ud = cloud_config(client_pub, host_priv, host_pub)
        kw: dict[str, Any] = {
            "name": self.name, "flavor_id": flavor["id"], "networks": [{"uuid": net["id"]}],
            "security_groups": sgs, "user_data": base64.b64encode(ud.encode()).decode(),
            "block_device_mapping": [{"boot_index": 0, "uuid": img["id"], "source_type": "image",
                                      "destination_type": "volume", "delete_on_termination": True,
                                      "volume_size": int(self.cfg.get("helper_root_gib", 10))}],
            "metadata": {MANAGED: "true", HELPER: "true", "vmigrate:run": self.ctx.run_id},
        }
        if self.cfg.get("helper_availability_zone") or self.cfg.get("availability_zone"):
            kw["availability_zone"] = self.cfg.get("helper_availability_zone") or self.cfg.get("availability_zone")
        self.ctx.logger.info("helper VM %s: booting image %s on network %s", self.name, img.get("name"), net.get("name"))
        srv = self.api.create_server(**kw)
        self.server = poll_until(lambda: self.api.get_server(srv["id"]), lambda s: s.get("status") == "ACTIVE",
                                 failed=lambda s: s.get("status") == "ERROR", timeout=self.op_timeout,
                                 interval=self.poll, what=f"helper VM {self.name} ACTIVE")
        host = self._address(net)
        t = self._tf(host, self.dir / "id_ed25519", known, port=int(self.cfg.get("helper_ssh_port", 22)))
        self._wait_ssh(t, host)
        if not t.run("command -v python3 || true").strip():
            raise ProviderError(f"helper image {self.cfg['helper_image']!r} has no python3; use a standard cloud "
                                "image (Ubuntu, Rocky/Alma, Debian)")
        self.transport = t
        self.ctx.logger.info("helper VM %s ready at %s", self.name, host)
        return t

    def _flavor(self) -> dict[str, Any]:
        if self.cfg.get("helper_flavor"):
            return self.api.find_flavor(self.cfg["helper_flavor"])
        fits = [f for f in self.api.flavors() if f.get("vcpus", 0) >= 2 and f.get("ram", 0) >= 2048]
        fits.sort(key=lambda f: (f.get("vcpus", 0), f.get("ram", 0)))
        if not fits:
            raise ProviderError("no flavor with >= 2 vCPU and >= 2 GiB RAM for the helper VM; set helper_flavor")
        return fits[0]

    def _address(self, net: dict[str, Any]) -> str:
        fnet = self.cfg.get("helper_floating_network")
        if fnet:
            ports = self.api.server_ports(self.server["id"])
            if not ports:
                raise ProviderError(f"helper VM {self.name} has no port for a floating IP")
            fip = self.api.create_floating_ip(self.api.find_network(fnet)["id"], ports[0]["id"])
            self.fip_id = fip["id"]
            return fip["floating_ip_address"]
        addrs = (self.server.get("addresses") or {})
        for a in addrs.get(net.get("name"), []) + [x for v in addrs.values() for x in v]:
            if a.get("version", 4) == 4 and a.get("addr"):
                return a["addr"]
        raise ProviderError(f"helper VM {self.name} has no IPv4 address on {net.get('name')}")

    def _wait_ssh(self, t: Any, host: str) -> None:
        deadline = time.monotonic() + float(self.cfg.get("helper_boot_timeout", 600))
        last = ""
        while time.monotonic() < deadline:
            try:
                t.run("true", timeout=30)
                return
            except (ProviderError, subprocess.TimeoutExpired) as e:
                last = str(e)
                time.sleep(self.poll)
        raise ProviderError(f"helper VM {self.name}: no SSH to {host}:{self.cfg.get('helper_ssh_port', 22)} within "
                            f"{self.cfg.get('helper_boot_timeout', 600)}s. Check that this worker can reach it and that the "
                            f"helper's security group(s) {self.security_groups()} allow TCP/22 from this worker "
                            f"(helper_network / helper_floating_network / helper_security_groups). "
                            f"Last error: {last[:400]}")

    def _delete_leftover(self) -> None:
        old = self.api.find_server(self.name)
        if not old:
            return
        if (self.api.get_server(old["id"]).get("metadata") or {}).get(HELPER) != "true":
            raise SafetyGateError(f"a server named {self.name!r} exists and is not a vmigrate helper")
        self.ctx.logger.warning("deleting leftover helper VM %s from an interrupted run", self.name)
        self.api.delete_server(old["id"])
        poll_until(lambda: self.api.find_server(self.name), lambda x: x is None,
                   timeout=self.op_timeout, interval=self.poll, what="leftover helper delete")
        sg = self.api.find_security_group(f"{self.name}-ssh")
        if sg:
            self.api.delete_security_group(sg["id"])

    # ------------------------------------------------------------------ volumes
    def attach(self, volume_id: str) -> str:
        with self._lock:
            return self._attach(volume_id)

    def _attach(self, volume_id: str) -> str:
        if volume_id in self.attached:
            return self.attached[volume_id]
        t = self.ensure()
        vol = self.api.get_volume(volume_id)
        if (vol.get("metadata") or {}).get(MANAGED) != "true":
            raise SafetyGateError(f"refusing to attach volume {volume_id} to the helper: not vmigrate-managed")
        self.ctx.require_execute("attach volume to helper VM", volume=volume_id, helper=self.name)
        self.api.attach_volume(self.server["id"], volume_id)
        poll_until(lambda: self.api.get_volume(volume_id), lambda v: v.get("status") == "in-use",
                   failed=lambda v: str(v.get("status", "")).startswith("error"),
                   timeout=self.op_timeout, interval=self.poll, what=f"volume {volume_id} attached to helper")
        dev = poll_until(
            lambda: find_device(t.run("ls -1 /dev/disk/by-id/ 2>/dev/null || true"),
                                t.run("lsblk -dno NAME,SERIAL 2>/dev/null || true"), volume_id),
            lambda d: d is not None, timeout=float(self.cfg.get("helper_device_timeout", 180)),
            interval=max(1.0, self.poll / 2), what=f"device for volume {volume_id} inside the helper")
        self.attached[volume_id] = dev
        self.ctx.logger.info("volume %s attached to helper as %s", volume_id, dev)
        return dev

    def detach(self, volume_id: str) -> None:
        with self._lock:
            self._detach(volume_id)

    def _detach(self, volume_id: str) -> None:
        if volume_id not in self.attached or self.server is None:
            return
        if self.transport is not None:
            try:
                self.transport.run(f"sync; sudo -n blockdev --flushbufs {shlex.quote(self.attached[volume_id])} || true")
            except ProviderError:
                pass
        self.api.detach_volume(self.server["id"], volume_id)
        poll_until(lambda: self.api.get_volume(volume_id), lambda v: v.get("status") == "available",
                   failed=lambda v: str(v.get("status", "")).startswith("error"),
                   timeout=self.op_timeout, interval=self.poll, what=f"volume {volume_id} detached from helper")
        del self.attached[volume_id]

    def detach_all(self) -> None:
        for vid in list(self.attached):
            self.detach(vid)

    def destroy(self) -> None:
        with self._lock:
            self._destroy()

    def _destroy(self) -> None:
        """Best effort, every step: a failing API call must not leave the rest behind. Errors are
        collected and raised at the end with the names to remove by hand."""
        if self.server is None and not self.dir.exists():
            return
        errors: list[str] = []

        def step(what: str, fn: Callable[[], Any]) -> None:
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - keep cleaning
                errors.append(f"{what}: {e}")
                self.ctx.logger.warning("helper cleanup: %s failed: %s", what, e)

        step("detach volumes", self.detach_all)
        if self.server is not None:
            # every helper of this run (a retried create may have made a second one)
            def delete_all() -> None:
                for s in self.api.servers_by_name(self.name):
                    if (s.get("metadata") or {}).get(HELPER) == "true":
                        self.api.delete_server(s["id"])
                poll_until(lambda: self.api.servers_by_name(self.name), lambda x: not x,
                           timeout=self.op_timeout, interval=self.poll, what="helper VM delete")
            step(f"delete helper VM {self.name}", delete_all)
            self.server = None
        if self.fip_id:
            step("delete floating IP", lambda: self.api.delete_floating_ip(self.fip_id))
            self.fip_id = None
        if self.sg_id:
            step("delete temporary security group", lambda: self.api.delete_security_group(self.sg_id))
            self.sg_id = None
        self.transport = None
        shutil.rmtree(self.dir, ignore_errors=True)
        if errors:
            raise ProviderError(f"helper VM cleanup incomplete; remove by hand anything named {self.name}*: "
                                + "; ".join(errors))
            self.ctx.logger.info("helper VM %s removed", self.name)
