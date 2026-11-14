"""Purpose: generic OpenStack target = the VHI target forced into helper_vm mode (no vStorage), with the
real NBD agent run locally against per-volume files (fakes from test_vhi_helper).

Usage: pytest tests/test_target_openstack.py
"""

from __future__ import annotations

import logging

from vmigrate.config.loader import EngineConfig, MigrationConfig
from vmigrate.core.context import RunContext
from vmigrate.engine import MigrationRunner, Phase, StateStore
from vmigrate.providers import TARGETS, build_target
from vmigrate.providers.fake.provider import FakeSource
from vmigrate.providers.openstack.target import OpenStackTarget

from .conftest import fake_vm
from .fakes_platform import FakeQemu
from .test_vhi_helper import FakeHelperOpenStack, fake_keygen, make_transport


def _tgt(tmp_path, ctx, **cfg):
    (tmp_path / "vstorage" / "vols/datastores/cinder").mkdir(parents=True)   # even WITH a vStorage mount here
    api = FakeHelperOpenStack(tmp_path / "vstorage", tmp_path / "devs")
    conf = {"type": "openstack", "project_id": "proj-1", "vstorage_mount": str(tmp_path / "vstorage"),
            "staging_dir": str(tmp_path / "staging"), "poll_interval": 0, "op_timeout": 5,
            "helper_image": "ubuntu-22.04", "helper_network": "helper-net", "network_map": {"net0": "uuid-net0"},
            "write_mode": "vstorage", **cfg}
    t = OpenStackTarget("os", conf, ctx, api=api, qemu=FakeQemu())
    t.helper._tf = make_transport(api, [])
    t.helper._keygen = fake_keygen
    return t, api


def test_registered_and_always_helper_mode(tmp_path):
    assert TARGETS["openstack"][1] == "implemented"
    ctx = RunContext(dry_run=True, logger=logging.getLogger("t"), run_id="r")
    t, _ = _tgt(tmp_path, ctx)
    assert t.write_mode == "helper_vm" and t.type_name == "openstack" and t.platform_family == "kvm"
    assert isinstance(build_target("os", {"type": "openstack", "auth_url": "https://k:5000/v3"}, ctx), OpenStackTarget)
    assert any("helper_image" in p for p in OpenStackTarget("o", {"type": "openstack"}, ctx, api=t.api).check_connection())


def test_full_migration_to_openstack(tmp_path):
    ctx = RunContext(dry_run=False, logger=logging.getLogger("os-e2e"), run_id="run-abcdef123456")
    src = FakeSource("src", {"type": "fake", "vms": [fake_vm(disks=3, size="3MiB")]}, ctx)
    t, api = _tgt(tmp_path, ctx)
    eng = EngineConfig(state_dir=tmp_path / "state", report_dir=tmp_path / "rep", chunk_size=1 << 20,
                       verify_samples=4, verify_sample_bytes=64 << 10, boot_timeout=1, max_parallel_disks=2,
                       verify_full=True)
    job = MigrationConfig(name="m", source="src", target="os", vm="RIS-Sainath", osmorph={"allow_manual": True},
                          target_options={"network_map": {}})
    st = MigrationRunner(job, eng, src, t, ctx, StateStore(eng.state_dir)).run()
    t.close()
    assert st.phase == Phase.DONE, st.errors
    assert all(d.verify["full"]["ok"] for d in st.disks.values())
    vm = next(s for s in api.servers.values() if not s["name"].startswith("vmigrate-helper"))
    assert len(vm["kw"]["block_device_mapping"]) == 3
    assert not list((tmp_path / "staging").glob("*.raw"))                 # never staged / never vStorage
