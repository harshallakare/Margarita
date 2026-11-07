"""Purpose: Intermediate Representation package. Import the IR types from here.

Usage: from vmigrate.ir import VmSpec, DiskSpec, NicSpec, DiskBus, Firmware, GuestOS, OsFamily, CpuSpec
"""

from .model import (
    CpuSpec,
    DiskBus,
    DiskSpec,
    Firmware,
    GuestOS,
    IRValidationError,
    NicSpec,
    OsFamily,
    VmSpec,
)

__all__ = [
    "CpuSpec",
    "DiskBus",
    "DiskSpec",
    "Firmware",
    "GuestOS",
    "IRValidationError",
    "NicSpec",
    "OsFamily",
    "VmSpec",
]
