"""Purpose: the stable provider contracts. The engine talks ONLY to these; every platform adapter
implements one SourceProvider and/or one TargetProvider. Providers never import each other.

Usage (implementing a new platform): subclass SourceProvider / TargetProvider, implement every
abstractmethod, keep the platform SDK import inside your provider package, and register it in
vmigrate.providers.REGISTRY. See README "Adding a provider".

Contract highlights:
  * inspect_vm MUST enumerate ALL disks — no caps, follow pagination.
  * disk_count_crosscheck MUST return counts from >= 2 independent enumeration paths; the engine
    fails loud if they disagree with each other or with inspect_vm.
  * open_disk only accepts a SnapshotRef — there is no API to read a live disk.
  * every method that changes state (on either side) calls self.ctx.require_execute(...) first.
  * source-side mutation is limited to creating/removing the snapshot artefacts the provider
    itself created; source VMs and disks are never modified or deleted.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ir import DiskBus, DiskSpec, Firmware, VmSpec
from .context import RunContext
from .ranges import ByteRange

# --------------------------------------------------------------------------- references


@dataclass(frozen=True)
class VmRef:
    id: str
    name: str
    provider: str = ""


@dataclass(frozen=True)
class SnapshotRef:
    """A frozen, readable copy of a VM's disks. `data` is provider-private and JSON-safe so it can
    be persisted in the state file and used to resume / clean up later."""

    id: str
    vm: VmRef
    app_consistent: bool
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckpointRef:
    id: str
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TargetDiskRef:
    id: str
    name: str
    size_bytes: int
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DiskHints:
    bus: DiskBus
    is_boot: bool = False
    thin: bool = True
    storage_pool: str | None = None
    order: int = 0
    shared: bool = False             # multi-attach volume (cluster shared disk)


@dataclass
class BootCheck:
    ok: bool
    detail: str
    evidence: str = ""


# --------------------------------------------------------------------------- byte seams


class DiskReader(ABC):
    """Random/sequential reads of a frozen disk's raw bytes."""

    @property
    @abstractmethod
    def size(self) -> int: ...

    @abstractmethod
    def read(self, offset: int, length: int) -> bytes:
        """Return exactly `length` bytes (short only at EOF)."""

    def allocated_ranges(self) -> list[ByteRange] | None:
        """Optional: ranges that may contain data. None = unknown (copy everything).
        Implementations must NOT probe holes where that is known to hang (Nutanix NFS)."""
        return None

    def close(self) -> None:
        pass

    def __enter__(self) -> DiskReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class DiskWriter(ABC):
    """Writes raw bytes at offsets into a target disk (or its local staging area)."""

    #: True when unwritten regions are guaranteed to read back as zeros (new sparse file /
    #: freshly created thin volume). Lets the copier skip all-zero chunks.
    prezeroed: bool = False

    @property
    @abstractmethod
    def size(self) -> int: ...

    @abstractmethod
    def write(self, offset: int, buf: bytes) -> None: ...

    def write_zeroes(self, offset: int, length: int) -> None:
        if self.prezeroed:
            return
        step = 8 << 20
        zero = bytes(min(step, length))
        off = offset
        while off < offset + length:
            n = min(step, offset + length - off)
            self.write(off, zero[:n])
            off += n

    def flush(self) -> None:
        """Make all writes so far durable. The engine only persists a resume watermark after
        flush() returns."""

    def close(self) -> None:
        self.flush()

    @property
    def staged_path(self) -> Path | None:
        """Local raw image the writer is filling, if any (used by OSMorph + verification)."""
        return None

    def __enter__(self) -> DiskWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------- providers


class _Provider(ABC):
    #: registry key, e.g. "nutanix", "vhi"
    type_name: str = ""

    def __init__(self, name: str, config: Mapping[str, Any], ctx: RunContext):
        self.name = name
        self.config = config
        self.ctx = ctx

    def preflight(self, spec: VmSpec | None = None) -> list[str]:
        """Return human-readable blocking problems (empty list = ready). Read-only. Runs on the
        WORKER, so it may check worker-host prerequisites (mounts, local tools, sockets)."""
        return []

    def check_connection(self) -> list[str]:
        """Save-time validation of an endpoint: reach the platform and prove the credentials (and
        named objects like a project/protection domain) work. Read-only and fast. Must NOT check
        worker-host prerequisites: the API that saves endpoints may run on another machine.
        Returns problems (empty = OK). Every adapter overrides this."""
        return [f"{self.type_name}: connection check not implemented"]

    def close(self) -> None:
        pass


class SourceProvider(_Provider):
    @abstractmethod
    def list_vms(self) -> list[VmRef]: ...

    # ---- cutover (optional) ---------------------------------------------------------------
    def shutdown_vm(self, vm: VmRef, *, force: bool = False, timeout: int = 900) -> str:
        """Power the SOURCE VM off for a cutover: guest (ACPI/tools) shutdown, or hard power-off when
        force. The ONLY call that changes a source VM's state; used only when the operator opts in
        (replica cutover with shutdown_source). Returns a short description. Idempotent: an
        already-off VM returns immediately."""
        raise NotImplementedError(f"{self.type_name}: source shutdown is not supported; power the VM off "
                                  "yourself, then run the cutover without shutdown_source")

    @abstractmethod
    def find_vm(self, name_or_id: str) -> VmRef: ...

    @abstractmethod
    def inspect_vm(self, vm: VmRef) -> VmSpec:
        """Full IR: specs + ALL disks + NICs + guest OS. Never capped."""

    @abstractmethod
    def disk_count_crosscheck(self, vm: VmRef) -> dict[str, int]:
        """Disk counts from >= 2 independent enumeration methods, keyed by method name."""

    @abstractmethod
    def create_snapshot(self, vm: VmRef, app_consistent: bool) -> SnapshotRef: ...

    @abstractmethod
    def open_disk(self, snapshot: SnapshotRef, disk: DiskSpec) -> DiskReader:
        """Reader over the disk's bytes AS OF the snapshot. Must refuse live disks."""

    def snapshot_disk_count(self, snapshot: SnapshotRef) -> int | None:
        """Optional extra gate: number of disks visible in the materialized snapshot."""
        return None

    def supports_cbt(self) -> bool:
        return False

    def checkpoint(self, snapshot: SnapshotRef) -> CheckpointRef:
        """Change-tracking reference for `snapshot`, persisted between replica syncs and passed back
        to changed_regions(base, cur). Default: the snapshot itself."""
        return CheckpointRef(id=snapshot.id, data=dict(snapshot.data))

    def keep_for_cbt(self) -> bool:
        """Whether the last sync's snapshot must stay on the source as the next diff base."""
        return self.supports_cbt()

    def retire_snapshot(self, snapshot: SnapshotRef, previous: CheckpointRef | None) -> CheckpointRef:
        """End of a replica sync when keep_for_cbt() is False: release `snapshot` and return the
        checkpoint the NEXT sync diffs against. Default: remove the snapshot, keep its checkpoint.
        (Hyper-V converts the snapshot into an RCT reference point here instead.)"""
        cp = self.checkpoint(snapshot)
        self.cleanup(snapshot)
        return cp

    def release_checkpoint(self, checkpoint: CheckpointRef) -> None:
        """Replica deleted: drop any source-side artefact behind `checkpoint` (e.g. a reference point)."""

    def changed_regions(
        self, disk: DiskSpec, base: CheckpointRef, cur: CheckpointRef
    ) -> list[ByteRange]:
        raise NotImplementedError(f"{self.type_name}: changed-region tracking not supported")

    @abstractmethod
    def cleanup(self, snapshot: SnapshotRef) -> None:
        """Remove ONLY artefacts this provider created for `snapshot`."""


class TargetProvider(_Provider):
    #: OSMorph key — the driver family the destination needs: "kvm", "vmware", "aws", "azure"
    platform_family: str = ""

    @abstractmethod
    def create_disk(self, name: str, size_bytes: int, hints: DiskHints) -> TargetDiskRef: ...

    @abstractmethod
    def find_disk(self, name: str) -> TargetDiskRef | None:
        """Idempotency: return an existing disk this tool created under `name`, if any."""

    @abstractmethod
    def open_disk_writer(self, disk: TargetDiskRef, *, resume: bool = False, incremental: bool = False) -> DiskWriter:
        """Writer for `disk`. incremental=True (replica sync): the writer starts with the disk's
        CURRENT committed content, so only changed ranges need writing; commit_disk then publishes it."""

    # ---- shared (multi-attach) disks for clusters (optional) ------------------------------
    def supports_shared_disks(self) -> tuple[bool, str]:
        """(True, "") when create_disk(hints.shared=True) makes a disk several VMs can attach at once."""
        return False, f"{self.type_name}: shared (multi-attach) disks are not supported"

    # ---- resources for the wizard's mapping dropdowns (optional) --------------------------
    def list_networks(self) -> list[dict[str, Any]]:
        """[{id, name, ...}] the target can attach VMs to. Empty = not supported (free text)."""
        return []

    def list_storage(self) -> list[dict[str, Any]]:
        """[{id, name, ...}] storage pools / volume types the target can create disks on."""
        return []

    def list_flavors(self) -> list[dict[str, Any]]:
        """[{id, name, vcpus, ram_mb}] VM sizes, if the target uses predefined sizes."""
        return []

    def list_images(self) -> list[dict[str, Any]]:
        """[{id, name}] bootable images (e.g. for a helper VM). Empty = not applicable."""
        return []

    def list_security_groups(self) -> list[dict[str, Any]]:
        """[{id, name}] security groups (e.g. for a helper VM). Empty = not applicable."""
        return []

    # ---- helper VM kept between replica syncs (optional) -------------------------------------
    def keep_helper(self) -> dict[str, Any] | None:
        """End of a sync: hand the open helper (with its volumes still attached) over to a later run
        instead of destroying it in close(). Returns a JSON record, or None when nothing is kept."""
        return None

    def adopt_helper(self, record: dict[str, Any]) -> bool:
        """Start of a sync: reuse a helper kept by keep_helper(). False: not usable here (the caller
        then calls release_helper and a fresh helper is created on demand)."""
        return False

    def release_helper(self, record: dict[str, Any], *, wait: bool = True) -> None:
        """Remove a kept helper (its volumes are detached and left intact). Safe to repeat."""

    # ---- replica support (optional) -------------------------------------------------------
    def supports_replica(self) -> bool:
        """True when open_disk_writer(incremental=True) and clone_disk are implemented."""
        return False

    def clone_disk(self, disk: TargetDiskRef, name: str, restore_point: str | None = None) -> TargetDiskRef:
        """Deploy: a NEW managed disk with the content of `disk` (or of one of its restore points).
        The replica disk itself keeps receiving syncs."""
        raise NotImplementedError(f"{self.type_name}: clone_disk not supported")

    def supports_restore_points(self) -> tuple[bool, str]:
        """(supported, reason). Restore points are point-in-time copies of replica disks."""
        return False, f"{self.type_name}: restore points not implemented"

    def snapshot_disk(self, disk: TargetDiskRef, name: str) -> str:
        raise NotImplementedError

    def delete_disk_snapshot(self, disk: TargetDiskRef, snapshot_id: str) -> None:
        raise NotImplementedError

    def delete_disk(self, disk: TargetDiskRef) -> None:
        """Delete a disk this tool created (replica delete). Must refuse unmanaged disks."""
        raise NotImplementedError(f"{self.type_name}: delete_disk not supported")

    def commit_disk(self, disk: TargetDiskRef, writer_staged: Path | None) -> None:
        """Finalize a written disk (e.g. convert staged raw into the volume backing)."""

    def open_disk_reader(self, disk: TargetDiskRef) -> DiskReader | None:
        """Optional read-back for checksum verification (after commit)."""
        return None

    def discard_staging(self, disk: TargetDiskRef) -> None:
        """Free local staging for a committed + verified disk."""

    def morph_image(self, disk: TargetDiskRef) -> tuple[Path, str] | None:
        """(local path, format) of the committed disk for offline OSMorph, or None if the target
        cannot expose it (OSMorph then degrades to manual)."""
        return None

    def prepare_boot_disk(self, disk: TargetDiskRef, spec: VmSpec, bus: DiskBus) -> None:
        """Called before create_vm: platform metadata the boot disk needs (bus, firmware)."""

    def target_bus(self, spec: VmSpec) -> DiskBus:
        """Bus the target will present the boot disk on (drives the OSMorph decision)."""
        return spec.boot_disk().bus

    @abstractmethod
    def create_vm(self, spec: VmSpec, boot_disk: TargetDiskRef, data_disks: list[TargetDiskRef]) -> VmRef:
        """Create the VM. A target MAY attach `data_disks` (in order) at creation so the guest sees
        every disk on first boot; the engine then attaches whatever list_attached_disks lacks."""

    @abstractmethod
    def find_vm(self, name: str) -> VmRef | None: ...

    @abstractmethod
    def attach_disk(self, vm: VmRef, disk: TargetDiskRef, order: int) -> None: ...

    @abstractmethod
    def list_attached_disks(self, vm: VmRef) -> list[str]:
        """Target disk ids attached to the VM, in device order."""

    def set_boot(self, vm: VmRef, boot_disk: TargetDiskRef, firmware: Firmware, bus: DiskBus) -> None:
        """After attach: ensure boot order/firmware. No-op where prepare_boot_disk covered it."""

    def check_boot(self, vm: VmRef, timeout: float = 900, *, guest_os: str | None = None,
                   progress: Any = None) -> BootCheck:
        """guest_os: "linux" | "windows" | None; progress(msg) reports waiting status."""
        return BootCheck(ok=True, detail="boot check not implemented for this target")

    @abstractmethod
    def cleanup_on_failure(self, created: Mapping[str, Any]) -> None:
        """Delete ONLY resources recorded in `created` (state file) by this tool."""
