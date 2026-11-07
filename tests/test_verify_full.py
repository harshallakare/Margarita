"""Purpose: optional full-disk SHA-256 verification (engine.verify_full) — unit and engine level.

Usage: pytest tests/test_verify_full.py
"""

from __future__ import annotations

import hashlib

import pytest

from vmigrate.core.errors import VerificationError
from vmigrate.engine import Phase
from vmigrate.engine.verify import full_digest, full_verify
from vmigrate.providers.fake.provider import PatternDisk


class _Flip:
    """Reader that returns `inner`'s bytes with one byte flipped at `at` (a silent corruption)."""

    def __init__(self, inner, at: int):
        self.inner, self.at = inner, at

    def read(self, off: int, n: int) -> bytes:
        b = bytearray(self.inner.read(off, n))
        if off <= self.at < off + n:
            b[self.at - off] ^= 0xFF
        return bytes(b)

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        self.close()


def test_full_digest_matches_hashlib_and_ignores_target_padding():
    d = PatternDisk("x", 3 << 20)
    assert full_digest(d, 3 << 20, chunk=1 << 20) == hashlib.sha256(d.read(0, 3 << 20)).hexdigest()
    big = PatternDisk("x", 4 << 20)          # target rounded up: only the source size is compared
    assert full_verify(d, big, 3 << 20, chunk=1 << 19)["ok"]
    bad = full_verify(d, _Flip(d, (3 << 20) - 1), 3 << 20)
    assert not bad["ok"] and bad["source_sha256"] != bad["target_sha256"]
    assert full_verify(d, None, 10)["skipped"]


def test_engine_full_verify_records_digest(make_runner):
    runner, src, tgt = make_runner(disks=3, engine={"verify_full": True})
    st = runner.run()
    assert st.phase == Phase.DONE
    for i in range(3):
        full = st.disks[f"RIS-Sainath-d{i}"].verify["full"]
        assert full["ok"] and full["source_sha256"] == full["target_sha256"] and full["bytes"] == 9 << 20


def test_engine_full_verify_catches_what_sampling_misses(make_runner):
    runner, src, tgt = make_runner(disks=2, engine={"verify_full": True})
    orig = tgt.open_disk_reader
    calls: dict[str, int] = {}

    def reader(ref):
        r = orig(ref)
        calls[ref.name] = calls.get(ref.name, 0) + 1
        # sampled checks (1st read-back of a disk) see it clean; the full pass (2nd) sees one flipped byte
        return _Flip(r, 5 * (1 << 20) + 7) if r is not None and calls[ref.name] == 2 else r
    tgt.open_disk_reader = reader
    with pytest.raises(VerificationError, match="full checksum mismatch"):
        runner.run()


def test_engine_full_verify_off_by_default(make_runner):
    runner, src, tgt = make_runner(disks=1)
    st = runner.run()
    assert "full" not in st.disks["RIS-Sainath-d0"].verify
