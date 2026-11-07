"""Purpose: provider-agnostic core — contracts, refs, run context, errors, byte-range math.

Usage: from vmigrate.core import SourceProvider, TargetProvider, DiskReader, DiskWriter, RunContext
"""

from .context import PlannedAction, RunContext
from .errors import (
    ConfigError,
    DiskCountMismatch,
    DryRunViolation,
    LiveDiskReadRefused,
    ManualMorphRequired,
    MigrationError,
    ProviderError,
    ProviderNotImplemented,
    SafetyGateError,
    TransientProviderError,
    VerificationError,
)
from .interfaces import (
    BootCheck,
    CheckpointRef,
    DiskHints,
    DiskReader,
    DiskWriter,
    SnapshotRef,
    SourceProvider,
    TargetDiskRef,
    TargetProvider,
    VmRef,
)
from .ranges import ByteRange

__all__ = [n for n in dir() if not n.startswith("_")]
