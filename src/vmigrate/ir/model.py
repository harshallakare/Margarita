"""Purpose: the normalized VM model (IR). Sources map platform VMs INTO VmSpec; targets map
VmSpec OUT to their platform. Nothing here knows about any platform.

Usage:
    spec = VmSpec(name=..., source_id=..., cpu=CpuSpec(2, 4), memory_bytes=..., disks=[...])
    spec.validate()                 # raises IRValidationError on an inconsistent model
    d = spec.to_dict(); VmSpec.from_dict(d)   # JSON round-trip for the state store

Invariants enforced by validate():
  * at least one disk, exactly one boot disk
  * disk ids and orders unique; orders are 0..n-1 (no gaps -> no silently dropped disk)
  * sizes positive
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class IRValidationError(ValueError):
    """The VmSpec is internally inconsistent."""


class Firmware(StrEnum):
    BIOS = "bios"
    UEFI = "uefi"


class DiskBus(StrEnum):
    SCSI = "scsi"
    SATA = "sata"
    IDE = "ide"
    VIRTIO = "virtio"
    NVME = "nvme"


class OsFamily(StrEnum):
    LINUX = "linux"
    WINDOWS = "windows"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CpuSpec:
    sockets: int
    cores: int  # cores per socket

    @property
    def total(self) -> int:
        return self.sockets * self.cores


@dataclass(frozen=True)
class GuestOS:
    family: OsFamily = OsFamily.UNKNOWN
    distro: str | None = None
    version: str | None = None
    bits: int = 64


@dataclass
class DiskSpec:
    id: str                      # stable id within the source (e.g. Nutanix vmdisk uuid)
    bus: DiskBus
    size_bytes: int
    order: int                   # 0-based attach order on the target
    is_boot: bool = False
    bus_index: int | None = None  # device index on its bus at the source (e.g. scsi.12)
    label: str | None = None
    source_handle: dict[str, Any] = field(default_factory=dict)  # provider-private, JSON-safe
    # Guest-visible controller model at the source: virtio-scsi, virtio-blk, pvscsi, lsilogic,
    # lsilogic-sas, buslogic, ahci, ide, nvme. OSMorph needs it: "scsi" on VMware (pvscsi) and
    # "scsi" on KVM (virtio-scsi) are different drivers. None = unknown (treated as non-virtio).
    controller: str | None = None

    @property
    def address(self) -> str:
        return f"{self.bus.value}.{self.bus_index}" if self.bus_index is not None else self.bus.value


@dataclass
class NicSpec:
    id: str
    network: str                 # source network name/id; mapped to a target network by config
    mac: str | None = None
    ipv4: str | None = None
    model: str | None = None     # virtio, e1000, vmxnet3 ...


@dataclass
class VmSpec:
    name: str
    source_id: str
    cpu: CpuSpec
    memory_bytes: int
    disks: list[DiskSpec]
    nics: list[NicSpec] = field(default_factory=list)
    firmware: Firmware = Firmware.BIOS
    secure_boot: bool = False
    guest_os: GuestOS = field(default_factory=GuestOS)
    machine_type: str | None = None
    cpu_flags: list[str] = field(default_factory=list)
    firmware_vars: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    # ---- queries -----------------------------------------------------------------
    def ordered_disks(self) -> list[DiskSpec]:
        return sorted(self.disks, key=lambda d: d.order)

    def boot_disk(self) -> DiskSpec:
        boots = [d for d in self.disks if d.is_boot]
        if len(boots) != 1:
            raise IRValidationError(f"{self.name}: expected exactly 1 boot disk, found {len(boots)}")
        return boots[0]

    def total_disk_bytes(self) -> int:
        return sum(d.size_bytes for d in self.disks)

    # ---- validation --------------------------------------------------------------
    def validate(self) -> None:
        errs: list[str] = []
        if not self.disks:
            errs.append("no disks")
        ids = [d.id for d in self.disks]
        if len(set(ids)) != len(ids):
            errs.append(f"duplicate disk ids: {sorted(i for i in set(ids) if ids.count(i) > 1)}")
        orders = sorted(d.order for d in self.disks)
        if orders != list(range(len(self.disks))):
            errs.append(f"disk orders must be exactly 0..{len(self.disks) - 1}, got {orders}")
        for d in self.disks:
            if d.size_bytes <= 0:
                errs.append(f"disk {d.id} has non-positive size {d.size_bytes}")
        boots = [d.id for d in self.disks if d.is_boot]
        if len(boots) != 1:
            errs.append(f"expected exactly 1 boot disk, found {len(boots)}: {boots}")
        if self.cpu.sockets < 1 or self.cpu.cores < 1:
            errs.append(f"invalid cpu {self.cpu}")
        if self.memory_bytes <= 0:
            errs.append("memory_bytes must be positive")
        if errs:
            raise IRValidationError(f"VmSpec {self.name!r} invalid: " + "; ".join(errs))

    # ---- serialization -----------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VmSpec:
        return cls(
            name=d["name"],
            source_id=d["source_id"],
            cpu=CpuSpec(**d["cpu"]),
            memory_bytes=d["memory_bytes"],
            disks=[DiskSpec(**{**x, "bus": DiskBus(x["bus"])}) for x in d["disks"]],
            nics=[NicSpec(**n) for n in d.get("nics", [])],
            firmware=Firmware(d.get("firmware", "bios")),
            secure_boot=d.get("secure_boot", False),
            guest_os=GuestOS(**{**d.get("guest_os", {}), "family": OsFamily(d.get("guest_os", {}).get("family", "unknown"))}),
            machine_type=d.get("machine_type"),
            cpu_flags=list(d.get("cpu_flags", [])),
            firmware_vars=d.get("firmware_vars"),
            extra=dict(d.get("extra", {})),
        )
