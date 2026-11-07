"""Purpose: prove the provider-agnostic engine end to end against the loopback fake pair:
13-disk migration, dry-run safety, the disk-count gate, resume, verification, OSMorph policy.

Usage: pytest tests/test_engine_fake.py
"""

from __future__ import annotations

import pytest

from vmigrate.core.errors import (
    DiskCountMismatch,
    DryRunViolation,
    ManualMorphRequired,
    SafetyGateError,
    VerificationError,
)
from vmigrate.core.interfaces import DiskHints
from vmigrate.engine import Phase, StateStore
from vmigrate.ir import DiskBus
from vmigrate.providers.fake.provider import PatternDisk


def test_full_13_disk_migration(make_runner, tmp_path):
    runner, src, tgt = make_runner(disks=13)
    st = runner.run()
    assert st.phase == Phase.DONE
    assert st.crosscheck == {"ir.inspect_vm": 13, "fake.primary": 13, "fake.secondary": 13}
    vm = tgt.vms["RIS-Sainath"]
    assert len(vm["disks"]) == 13
    # attach order == source order, including the 3 disks Coriolis cannot see (index 10..12)
    assert vm["disks"] == [f"vol-RIS-Sainath-disk{i:02d}" for i in range(13)]
    for i in range(13):
        ref = tgt.disks[f"RIS-Sainath-disk{i:02d}"]
        expected = PatternDisk(f"RIS-Sainath:{i}", ref.size_bytes)
        assert tgt._final(ref).read_bytes() == expected.read(0, ref.size_bytes)  # byte-identical
        assert not tgt._staged(ref).exists()  # staging freed after commit + verify
        ds = st.disks[f"RIS-Sainath-d{i}"]
        assert ds.copied and ds.committed and ds.attached and ds.verify["ok"]
        assert ds.verify["staged_samples"]["ok"] and ds.verify["target_samples"]["ok"]
        assert ds.copy_stats["bytes_zero_skipped"] > 0  # zero-skip exercised
    assert src.cleaned and not src.snapshots  # snapshot removed after success
    assert (tmp_path / "reports" / "ris.report.md").exists()


def test_dry_run_changes_nothing_and_plans_everything(make_runner, tmp_path):
    runner, src, tgt = make_runner(disks=13, execute=False)
    st = runner.run()
    assert st.phase == Phase.INSPECTED
    assert not src.snapshots and not tgt.disks and not tgt.vms and tgt.calls == []
    actions = [a.action for a in runner.ctx.planned]
    assert actions.count("target.create_disk") == 13
    assert "source.create_snapshot" in actions and "target.create_vm" in actions
    md = (tmp_path / "reports" / "ris.report.md").read_text()
    assert "Dry-run plan (nothing was changed)" in md


def test_mutating_provider_calls_refused_in_dry_run(make_runner):
    runner, src, tgt = make_runner(execute=False)
    with pytest.raises(DryRunViolation):
        tgt.create_disk("x", 1 << 20, DiskHints(bus=DiskBus.SCSI))
    with pytest.raises(DryRunViolation):
        src.create_snapshot(src.find_vm("RIS-Sainath"), app_consistent=True)


def test_disk_count_gate_fails_loud_before_any_action(make_runner):
    # Reproduce the Coriolis bug: a secondary enumeration that stops at 10 disks.
    runner, src, tgt = make_runner(disks=13, src_cfg={"secondary_enumeration_cap": 10})
    with pytest.raises(DiskCountMismatch) as ei:
        runner.run()
    assert ei.value.counts["fake.secondary"] == 10
    assert not src.snapshots and not tgt.disks  # nothing happened
    assert runner.state.phase == Phase.FAILED


def test_disk_count_gate_also_in_dry_run(make_runner):
    runner, *_ = make_runner(disks=13, execute=False, src_cfg={"secondary_enumeration_cap": 10})
    with pytest.raises(DiskCountMismatch):
        runner.run()


def test_resume_after_read_failure(make_runner, tmp_path):
    # 9 MiB disks; reads fail from 5 MiB on -> every disk checkpoints at 4 MiB then dies.
    runner, src, tgt = make_runner(disks=3, src_cfg={"fail_read_at": 5 << 20})
    with pytest.raises(IOError):
        runner.run()
    st = StateStore(tmp_path / "state").load("ris")
    assert st.phase == Phase.FAILED and st.last_good_phase == Phase.DISKS_CREATED
    marks = [d.bytes_copied for d in st.disks.values()]
    assert all(m == 4 << 20 for m in marks), marks
    snap_id = st.snapshot["id"]

    src.fail_read_at = None
    runner2, src2, tgt2 = make_runner(disks=3, reuse=True)
    st2 = runner2.run()
    assert st2.phase == Phase.DONE
    assert tgt2.calls.count("create_disk:RIS-Sainath-disk00") == 1  # disks not recreated
    assert snap_id in src2.cleaned  # same snapshot reused, then cleaned
    for d in st2.disks.values():
        assert d.copy_stats["resumed_from"] == 4 << 20
        assert d.verify["ok"]


def test_rerun_after_done_is_noop(make_runner):
    runner, src, tgt = make_runner(disks=2)
    runner.run()
    n = len(tgt.calls)
    runner2, *_ = make_runner(disks=2, reuse=True)
    assert runner2.run().phase == Phase.DONE
    assert len(tgt.calls) == n


def test_boot_check_failure_is_reported(make_runner):
    runner, *_ = make_runner(disks=2, tgt_cfg={"boot_ok": False})
    with pytest.raises(VerificationError, match="initramfs"):
        runner.run()
    assert runner.state.verification["boot"]["ok"] is False


def test_windows_to_kvm_requires_manual_morph_and_stops_before_vm(make_runner):
    runner, src, tgt = make_runner(disks=2, os="windows")
    with pytest.raises(ManualMorphRequired):
        runner.run()
    assert not tgt.vms
    assert runner.state.morph["status"] == "manual_required"


def test_manual_morph_can_be_explicitly_allowed(make_runner):
    runner, src, tgt = make_runner(disks=2, os="windows", job={"osmorph": {"allow_manual": True}})
    assert runner.run().phase == Phase.DONE


def test_refuses_to_adopt_foreign_target_vm(make_runner):
    runner, src, tgt = make_runner(disks=2)
    tgt.vms["RIS-Sainath"] = {"id": "someone-elses", "disks": []}
    with pytest.raises(SafetyGateError, match="not created by this migration"):
        runner.run()


def test_commit_failure_then_resume(make_runner):
    runner, src, tgt = make_runner(disks=3, tgt_cfg={"fail_commit_disk": "RIS-Sainath-disk01"})
    with pytest.raises(Exception, match="injected commit failure"):
        runner.run()
    st = runner.state
    assert st.last_good_phase == Phase.DISKS_CREATED
    assert st.disks["RIS-Sainath-d1"].copied and not st.disks["RIS-Sainath-d1"].committed
    assert st.disks["RIS-Sainath-d0"].committed and st.disks["RIS-Sainath-d2"].committed
    tgt.fail_commit = None
    runner2, *_ = make_runner(disks=3, reuse=True)
    assert runner2.run().phase == Phase.DONE
    # resumed run only committed the failed disk; it did not re-copy anything
    assert tgt.calls.count("commit:RIS-Sainath-disk01") == 1
    assert tgt.calls.count("commit:RIS-Sainath-disk00") == 1


def test_target_that_attaches_at_create(make_runner):
    runner, src, tgt = make_runner(disks=4, tgt_cfg={"attach_at_create": True})
    assert runner.run().phase == Phase.DONE
    assert not any(c.startswith("attach:") for c in tgt.calls)
    assert len(tgt.vms["RIS-Sainath"]["disks"]) == 4


def test_on_failure_cleanup_removes_only_created(make_runner):
    runner, src, tgt = make_runner(disks=2, tgt_cfg={"boot_ok": False}, engine={"on_failure": "cleanup"})
    tgt.disks["unrelated"] = "keep-me"
    with pytest.raises(VerificationError):
        runner.run()
    assert "cleanup" in tgt.calls
    assert list(tgt.disks) == ["unrelated"] and not tgt.vms
