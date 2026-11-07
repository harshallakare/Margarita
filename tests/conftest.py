"""Purpose: shared pytest fixtures — a fake source/target pair and a runner factory.

Usage: def test_x(make_runner): runner, src, tgt = make_runner(disks=13, execute=True)
"""

from __future__ import annotations

import logging

import pytest

from vmigrate.config.loader import EngineConfig, MigrationConfig
from vmigrate.core.context import RunContext
from vmigrate.engine import MigrationRunner, StateStore
from vmigrate.providers.fake.provider import FakeSource, FakeTarget


def fake_vm(name: str = "RIS-Sainath", disks: int = 13, size: str = "9MiB", os: str = "linux") -> dict:
    return {"name": name, "os": os, "disks": [{"size": size, "boot": i == 0, "index": i} for i in range(disks)]}


@pytest.fixture
def make_runner(tmp_path):
    shared: dict = {}

    def _make(*, disks: int = 13, execute: bool = True, src_cfg: dict | None = None, tgt_cfg: dict | None = None,
              engine: dict | None = None, job: dict | None = None, reuse: bool = False, os: str = "linux"):
        ctx = RunContext(dry_run=not execute, logger=logging.getLogger("test"))
        if reuse and shared:
            src, tgt = shared["src"], shared["tgt"]
            src.ctx = tgt.ctx = ctx
        else:
            src = FakeSource("src", {"type": "fake", "vms": [fake_vm(disks=disks, os=os)], **(src_cfg or {})}, ctx)
            tgt = FakeTarget("tgt", {"type": "fake", "staging_dir": str(tmp_path / "tgt"), **(tgt_cfg or {})}, ctx)
            shared.update(src=src, tgt=tgt)
        e = EngineConfig(state_dir=tmp_path / "state", report_dir=tmp_path / "reports",
                         chunk_size=1 << 20, checkpoint_every=2 << 20, verify_samples=8,
                         verify_sample_bytes=64 << 10, max_parallel_disks=4)
        for k, v in (engine or {}).items():
            setattr(e, k, v)
        j = MigrationConfig(name="ris", source="src", target="tgt", vm="RIS-Sainath", **(job or {}))
        return MigrationRunner(j, e, src, tgt, ctx, StateStore(e.state_dir)), src, tgt

    return _make
