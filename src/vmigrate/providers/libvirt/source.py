"""Purpose: KVM / libvirt SourceProvider: plain KVM hosts (RHEL, Ubuntu, Debian, SUSE ...) managed
by libvirt, reached over SSH with `virsh`. No libvirt-python on the worker.

Usage (endpoint config; the SSH key goes in the encrypted credentials):
    type: libvirt
    host: kvm1.example.com
    ssh_user: root                        # or a user with passwordless sudo for virsh / qemu-nbd
    ssh_private_key: "-----BEGIN OPENSSH PRIVATE KEY-----..."     # credential
    ssh_known_hosts: "kvm1.example.com ssh-ed25519 AAAA..."      # pinned host key
    uri: qemu:///system                   # libvirt connection URI on the host
    scratch_dir: /var/lib/libvirt/images  # running VMs: backup scratch files (must be usable by QEMU)
    backup_port: 10900                    # running VMs: first TCP port tried for the backup NBD server (localhost)

How it works:
  inspect  : virsh dumpxml + domblkinfo per disk -> IR (virtio-blk / virtio-scsi guests need no
             driver injection on KVM targets)
  gate     : dumpxml disks vs `virsh domblklist --details`
  refuse   : pass-through LUNs, network/volume disks of a shut-off VM (no direct read path)
  snapshot :
    shut off -> nothing is created: qemu-nbd --read-only opens each disk WITH the image lock, so the
                VM cannot be started while a disk is read (the lock is held per disk being read)
    running  -> libvirt pull-mode backup (virsh backup-begin, libvirt >= 7.2 / QEMU >= 4.2): QEMU
                serves a point-in-time view of every disk over NBD on 127.0.0.1 of the host; SSH
                forwards it here. app_consistent freezes the guest filesystems (guest agent) around
                backup-begin. A checkpoint vmigrate-<run> (dirty bitmaps) is created with it.
  read     : NBD; allocated ranges from base:allocation, so unallocated areas are skipped
  CBT      : running VMs: the backup is incremental from the newest vmigrate checkpoint; the
             changed ranges are the export's qemu:dirty-bitmap:backup-<disk>. Replicas keep the
             checkpoint (not the backup job) between syncs; the older one is deleted.
  cleanup  : virsh domjobabort (ends the backup; libvirt deletes the scratch files), and for a
             migration the checkpoint it created.
The source disks are never written: the backup only reads, the offline path is read-only.

Tested against a fake host shell and a fake NBD server only; NOT run on a real KVM host yet.
"""

from __future__ import annotations

import base64
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import zlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...core.context import RunContext
from ...core.errors import (ChangeTrackingUnavailable, ConfigError, LiveDiskReadRefused, ProviderError, SafetyGateError,
                            TransientProviderError)
from ...core.interfaces import CheckpointRef, DiskReader, SnapshotRef, SourceProvider, VmRef
from ...core.nbd import STATE_DIRTY, STATE_HOLE, NbdReader, NbdStatus
from ...core.ranges import ByteRange
from ...ir import DiskSpec, VmSpec
from . import mapping

PREFIX = "vmigrate-"


class BackupExport(DiskReader):
    """One disk of a running libvirt pull-mode backup, read over NBD (forwarded over SSH)."""

    def __init__(self, shell: Any, port: int, export: str, *, start_timeout: float = 30):
        self.shell, self.export = shell, export
        self._fwd: subprocess.Popen | None = None
        self._ldir = tempfile.mkdtemp(prefix="vmigrate-nbd-")
        os.chmod(self._ldir, 0o700)
        self.lsock = str(Path(self._ldir) / "nbd.sock")
        self.port = port
        try:
            if getattr(shell, "local", False):
                self._nbd = NbdReader.connect_tcp("127.0.0.1", port, export)
            else:
                self._fwd = shell.forward(self.lsock, f"127.0.0.1:{port}")
                deadline = time.monotonic() + start_timeout
                while True:
                    if self._fwd.poll() is not None:
                        raise ProviderError(f"ssh forward exited: {(self._fwd.stderr.read() if self._fwd.stderr else '')[-300:]}")
                    if Path(self.lsock).exists():
                        try:
                            self._nbd = NbdReader.connect_unix(self.lsock, export)
                            break
                        except OSError:
                            pass
                    if time.monotonic() > deadline:
                        raise TransientProviderError(f"backup NBD export {export} not reachable within {start_timeout}s")
                    time.sleep(0.2)
        except Exception:
            self.close()
            raise

    def status(self, context: str) -> NbdStatus:
        if self._fwd is None:
            return NbdStatus.connect_tcp("127.0.0.1", self.port, self.export, context)
        return NbdStatus.connect_unix(self.lsock, self.export, context)

    @property
    def size(self) -> int:
        return self._nbd.size

    def read(self, offset: int, length: int) -> bytes:
        return self._nbd.read(offset, length)

    def allocated_ranges(self) -> list[ByteRange] | None:
        try:
            with self.status("base:allocation") as s:
                return s.ranges(lambda f: not f & STATE_HOLE)
        except (OSError, ProviderError):
            return None

    def close(self) -> None:
        nbd = getattr(self, "_nbd", None)
        if nbd is not None:
            nbd.close()
            self._nbd = None
        if self._fwd is not None and self._fwd.poll() is None:
            self._fwd.terminate()
            try:
                self._fwd.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._fwd.kill()
        self._fwd = None
        shutil.rmtree(self._ldir, ignore_errors=True)


class LibvirtSource(SourceProvider):
    type_name = "libvirt"

    def __init__(self, name: str, config: Mapping[str, Any], ctx: RunContext, shell: Any = None,
                 offline_factory: Any = None, backup_factory: Any = None):
        super().__init__(name, config, ctx)
        if not config.get("host"):
            raise ConfigError(f"libvirt source {name}: missing 'host'")
        self._shell = shell
        self._offline_factory = offline_factory
        self._backup_factory = backup_factory
        self._vm: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ plumbing
    @property
    def shell(self) -> Any:
        if self._shell is None:
            from ..common.remote import SshNode
            c = self.config
            self._shell = SshNode(c["host"], c.get("ssh_user", "root"), c.get("ssh_private_key", ""),
                                  c.get("ssh_known_hosts", ""), port=int(c.get("ssh_port", 22)), who="libvirt")
        return self._shell

    def virsh(self, *args: str, check: bool = True) -> str:
        return self.shell.run(["virsh", "-q", "-c", self.config.get("uri") or "qemu:///system", *args], check=check)

    def close(self) -> None:
        if self._shell is not None and hasattr(self._shell, "close"):
            self._shell.close()

    def _put(self, path: str, text: str) -> None:
        b64 = base64.b64encode(text.encode()).decode()
        self.shell.run(["sh", "-c", f"umask 077; echo {b64} | base64 -d > {shlex.quote(path)}"])

    def _state(self, dom: str) -> str:
        out = self.virsh("domstate", dom).strip().splitlines()
        return out[0].strip() if out else "unknown"

    def _load(self, vm: VmRef) -> dict[str, Any]:
        dom = mapping.safe(vm.id, "domain name")
        xml = self.virsh("dumpxml", dom)
        caps = {}
        for d in mapping.domain_disks(xml):
            if d["target"]:
                caps[d["target"]] = mapping.domblkinfo_capacity(self.virsh("domblkinfo", dom, mapping.safe(d["target"], "disk target")))
        st = self._state(dom)
        d = {"xml": xml, "caps": caps, "state": st}
        self._vm[vm.id] = d
        return d

    def _spec(self, d: dict[str, Any]) -> VmSpec:
        try:
            return mapping.to_vmspec(d["xml"], d["caps"], state=d["state"])
        except mapping.LibvirtMappingError as e:
            raise SafetyGateError(str(e)) from e

    # ------------------------------------------------------------------ contract
    def check_connection(self) -> list[str]:
        try:
            self.virsh("version")
            self.virsh("list", "--all", "--name")
        except Exception as e:  # noqa: BLE001
            return [f"cannot run virsh on {self.config['host']} over SSH: {e}"]
        return []

    def preflight(self, spec: VmSpec | None = None) -> list[str]:
        p = []
        try:
            self.virsh("version")
        except Exception as e:  # noqa: BLE001
            return [f"cannot run virsh on {self.config['host']}: {e}"]
        out = self.shell.run(["sh", "-c", "command -v qemu-nbd >/dev/null && echo ok"], check=False)
        if "ok" not in out:
            p.append(f"{self.config['host']}: qemu-nbd not found (needed for shut-off VMs; package qemu-utils / qemu-img)")
        return p

    def list_vms(self) -> list[VmRef]:
        names = [n.strip() for n in self.virsh("list", "--all", "--name").splitlines() if n.strip()]
        return [VmRef(n, n, self.name) for n in names]

    def find_vm(self, name_or_id: str) -> VmRef:
        names = {v.id for v in self.list_vms()}
        if name_or_id in names:
            return VmRef(name_or_id, name_or_id, self.name)
        out = self.virsh("domname", mapping.safe(name_or_id, "domain"), check=False).strip()
        if out and out in names:                      # a UUID or numeric id
            return VmRef(out, out, self.name)
        raise ProviderError(f"libvirt: no domain {name_or_id!r} on {self.config['host']}")

    def inspect_vm(self, vm: VmRef) -> VmSpec:
        return self._spec(self._load(vm))

    def disk_count_crosscheck(self, vm: VmRef) -> dict[str, int]:
        d = self._vm.get(vm.id) or self._load(vm)
        return mapping.crosscheck(d["xml"], self.virsh("domblklist", mapping.safe(vm.id, "domain"), "--details"))

    def shutdown_vm(self, vm: VmRef, *, force: bool = False, timeout: int = 900) -> str:
        dom = mapping.safe(vm.id, "domain")
        if self._state(dom) == "shut off":
            return f"{vm.name} already shut off"
        self.ctx.require_execute("virsh shutdown" if not force else "virsh destroy", domain=dom)
        if force:
            self.virsh("destroy", dom)
            return f"{vm.name} stopped (hard)"
        self.virsh("shutdown", dom)
        deadline = time.monotonic() + timeout
        while self._state(dom) != "shut off":
            if time.monotonic() > deadline:
                raise TransientProviderError(f"{vm.name} did not shut down within {timeout}s")
            time.sleep(5)
        return f"{vm.name} shut down"

    # ------------------------------------------------------------------ snapshots
    def _checkpoints(self, dom: str) -> list[str]:
        out = self.virsh("checkpoint-list", dom, "--name", "--topological", check=False)
        return [n.strip() for n in out.splitlines() if n.strip().startswith(PREFIX)]

    def _port(self, tag: str) -> int:
        return int(self.config.get("backup_port") or 10900) + zlib.crc32(tag.encode()) % 500

    def create_snapshot(self, vm: VmRef, app_consistent: bool) -> SnapshotRef:
        dom = mapping.safe(vm.id, "domain")
        d = self._load(vm)
        spec = self._spec(d)
        tag = f"{PREFIX}{self.ctx.run_id}"
        disks = [x.source_handle | {"target": x.id} for x in spec.ordered_disks()]
        if d["state"] == "shut off":
            if app_consistent:
                raise SafetyGateError(f"{vm.name} is shut off; there is nothing to quiesce. Set app_consistent: false.")
            for x in disks:
                try:
                    mapping.offline_plan(x)
                except mapping.LibvirtMappingError as e:
                    raise SafetyGateError(str(e)) from e
            self.ctx.require_execute("read shut-off libvirt domain", domain=dom)
            return SnapshotRef(id=tag, vm=vm, app_consistent=True,
                               data={"domain": dom, "mode": "offline", "tag": tag})
        if d["state"] not in ("running", "paused"):
            raise SafetyGateError(f"{vm.name} is {d['state']}; it must be running or shut off")
        prev = self._checkpoints(dom)
        incremental = prev[-1] if prev else None
        port = self._port(tag)
        scratch = self.config.get("scratch_dir") or "/var/lib/libvirt/images"
        bpath, cpath = f"/run/vmigrate/{tag}.backup.xml", f"/run/vmigrate/{tag}.checkpoint.xml"
        self.ctx.require_execute("virsh backup-begin (pull mode)", domain=dom, port=port, checkpoint=tag,
                                 incremental=incremental, freeze=app_consistent)
        self.shell.run(["mkdir", "-p", "-m", "0700", "/run/vmigrate"])
        self._put(bpath, mapping.backup_xml(disks, port, scratch, tag, incremental))
        self._put(cpath, mapping.checkpoint_xml(tag, disks))
        frozen = False
        try:
            if app_consistent:
                try:
                    self.virsh("domfsfreeze", dom)
                    frozen = True
                except ProviderError as e:
                    raise SafetyGateError(f"{vm.name}: app-consistent backup needs the QEMU guest agent "
                                          f"(domfsfreeze failed: {e}). Install it, or set app_consistent: false.") from e
            try:
                self.virsh("backup-begin", dom, bpath, cpath)
            except ProviderError as e:
                if incremental and "checkpoint" in str(e).lower():
                    self.ctx.logger.warning("libvirt: incremental backup from %s refused (%s); taking a full one",
                                            incremental, e)
                    incremental = None
                    self._put(bpath, mapping.backup_xml(disks, port, scratch, tag))
                    self.virsh("backup-begin", dom, bpath, cpath)
                else:
                    raise SafetyGateError(f"{vm.name}: virsh backup-begin failed ({e}). Running VMs need libvirt >= 7.2 "
                                          "and QEMU >= 4.2; or shut the VM off and run again.") from e
        finally:
            if frozen:
                self.virsh("domfsthaw", dom, check=False)
            self.shell.run(["rm", "-f", bpath, cpath], check=False)
        return SnapshotRef(id=tag, vm=vm, app_consistent=app_consistent,
                           data={"domain": dom, "mode": "backup", "tag": tag, "port": port, "checkpoint": tag,
                                 "incremental_from": incremental})

    def snapshot_disk_count(self, snapshot: SnapshotRef) -> int | None:
        if snapshot.data.get("mode") != "backup":
            return None
        job = self.virsh("domjobinfo", snapshot.data["domain"], check=False)
        if "backup" not in job.lower():
            raise SafetyGateError(f"no backup job is running on {snapshot.data['domain']} (aborted?)")
        return None

    def open_disk(self, snapshot: SnapshotRef, disk: DiskSpec) -> DiskReader:
        if not snapshot.id.startswith(PREFIX):
            raise LiveDiskReadRefused("libvirt: reads require a vmigrate snapshot")
        dom = snapshot.data["domain"]
        target = mapping.safe(disk.source_handle["target"], "disk target")
        if snapshot.data.get("mode") == "offline":
            st = self._state(dom)
            if st != "shut off":
                raise SafetyGateError(f"{dom} was started ({st}) after the copy began; its disks are no longer frozen. "
                                      "Shut it off and run again, or let it run (it is then read through a backup).")
            plan = mapping.offline_plan(disk.source_handle)
            tag = f"{snapshot.id}-{target}"
            if self._offline_factory is not None:
                r = self._offline_factory(self.shell, plan, tag, disk)
            else:
                from ..common.remote import NodeExport
                r = NodeExport(self.shell, plan, tag=tag)
        else:
            if self._backup_factory is not None:
                r = self._backup_factory(self.shell, snapshot.data["port"], target, disk)
            else:
                r = BackupExport(self.shell, snapshot.data["port"], target)
        if r.size < disk.size_bytes:
            r.close()
            raise SafetyGateError(f"{disk.label}: exported size {r.size} < inspected {disk.size_bytes}")
        return r

    def _end_backup(self, snapshot: SnapshotRef) -> None:
        if snapshot.data.get("mode") == "backup":
            self.virsh("domjobabort", snapshot.data["domain"], check=False)

    def cleanup(self, snapshot: SnapshotRef) -> None:
        self.ctx.require_execute("end libvirt backup", domain=snapshot.data.get("domain"), tag=snapshot.id)
        self._end_backup(snapshot)
        cp = snapshot.data.get("checkpoint")
        if cp and cp.startswith(PREFIX):
            self.virsh("checkpoint-delete", snapshot.data["domain"], cp, check=False)

    # ------------------------------------------------------------------ CBT (checkpoints + dirty bitmaps)
    def supports_cbt(self) -> bool:
        return True

    def keep_for_cbt(self) -> bool:
        return False     # the backup job ends after each sync; the checkpoint (bitmap) is what persists

    def checkpoint(self, snapshot: SnapshotRef) -> CheckpointRef:
        return CheckpointRef(id=snapshot.data.get("checkpoint") or "", data={
            "domain": snapshot.data["domain"], "mode": snapshot.data.get("mode"), "port": snapshot.data.get("port"),
            "incremental_from": snapshot.data.get("incremental_from")})

    def retire_snapshot(self, snapshot: SnapshotRef, previous: CheckpointRef | None) -> CheckpointRef:
        cp = self.checkpoint(snapshot)
        self._end_backup(snapshot)            # keep this run's checkpoint as the next base
        if previous is not None and previous.id.startswith(PREFIX) and previous.id != cp.id:
            self.virsh("checkpoint-delete", previous.data["domain"], previous.id, check=False)
        return cp

    def release_checkpoint(self, checkpoint: CheckpointRef) -> None:
        if checkpoint.id.startswith(PREFIX):
            self.virsh("checkpoint-delete", checkpoint.data["domain"], checkpoint.id, check=False)

    def changed_regions(self, disk: DiskSpec, base: CheckpointRef, cur: CheckpointRef) -> list[ByteRange]:
        if cur.data.get("mode") != "backup":
            raise ChangeTrackingUnavailable("the VM was shut off: no backup job, so no dirty bitmap")
        if not base.id or cur.data.get("incremental_from") != base.id:
            raise ChangeTrackingUnavailable(f"backup is not incremental from {base.id or '(none)'} "
                                            f"(was {cur.data.get('incremental_from')})")
        target = mapping.safe(disk.source_handle["target"], "disk target")
        ctx = f"qemu:dirty-bitmap:backup-{target}"
        try:
            if self._backup_factory is not None:
                r = self._backup_factory(self.shell, cur.data["port"], target, disk)
            else:
                r = BackupExport(self.shell, cur.data["port"], target)
            try:
                with r.status(ctx) as s:
                    return s.ranges(lambda f: f & STATE_DIRTY)
            finally:
                r.close()
        except (OSError, ProviderError) as e:
            raise ChangeTrackingUnavailable(f"{disk.label}: dirty bitmap {ctx}: {e}") from e
