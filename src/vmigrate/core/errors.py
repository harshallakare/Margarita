"""Purpose: the framework's exception hierarchy. Engine code catches these by type to decide
fail-loud vs retry vs report.

Usage: raise DiskCountMismatch(vm, counts) ; except MigrationError: ...
"""

from __future__ import annotations


class MigrationError(Exception):
    """Base class for all vmigrate errors."""


class ConfigError(MigrationError):
    """Configuration is missing or invalid."""


class DryRunViolation(MigrationError):
    """A mutating operation was attempted while the run is in dry-run mode."""


class SafetyGateError(MigrationError):
    """A pre-flight or in-flight safety gate refused to continue. Never auto-retried."""


class DiskCountMismatch(SafetyGateError):
    """Two independent disk enumerations disagree. This is THE bug that motivated the tool
    (Coriolis silently dropping disks at SCSI index >= 10)."""

    def __init__(self, vm: str, counts: dict[str, int]):
        self.vm = vm
        self.counts = counts
        super().__init__(
            f"DISK COUNT MISMATCH for VM {vm!r}: independent enumerations disagree {counts}. "
            "Refusing to migrate — a disk would be silently dropped."
        )


class LiveDiskReadRefused(SafetyGateError):
    """Something tried to read a disk that is not a frozen snapshot/checkpoint copy."""


class ProviderError(MigrationError):
    """A provider operation failed (API error, timeout, unexpected platform state)."""


class TransientProviderError(ProviderError):
    """A provider operation failed in a way that is safe to retry."""


class ProviderNotImplemented(MigrationError):
    """The requested platform adapter is in the support matrix but not implemented yet."""


class VerificationError(MigrationError):
    """Post-copy / post-create verification failed."""


class ManualMorphRequired(MigrationError):
    """No automatic OSMorph strategy exists (or it cannot run here) for this guest/target."""


class ChangeTrackingUnavailable(ProviderError):
    """The source cannot answer "what changed" for this disk right now (e.g. VMware CBT was reset or
    the change id is unknown). Replica sync falls back to compare mode for that disk; never fatal."""


def is_transient(e: BaseException) -> bool:
    """Safe to retry after a pause: provider-declared transient errors and network-level failures
    (connection refused / reset / unreachable, connect and read timeouts), including those raised by
    requests and keystoneauth (OpenStack). Seen live: a network cut mid-copy surfaced as an NBD
    "connection lost" and then as Keystone ConnectTimeout / Nova "No route to host" on resume.
    TLS/certificate errors are NOT transient: retrying cannot fix a wrong certificate."""
    import ssl
    import subprocess
    if isinstance(e, TransientProviderError):
        return True
    names = {c.__name__ for c in type(e).__mro__}
    if isinstance(e, ssl.SSLError) or "SSLError" in names:
        return False
    if isinstance(e, (ConnectionError, TimeoutError, subprocess.TimeoutExpired)):
        return True
    try:
        import requests
        if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
            return True
    except ImportError:
        pass
    try:
        from keystoneauth1.exceptions import connection as ksa
        if isinstance(e, (ksa.ConnectFailure, ksa.ConnectTimeout, ksa.UnknownConnectionError)):
            return True
    except ImportError:
        pass
    return False
