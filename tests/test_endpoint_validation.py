"""Purpose: endpoints are saved ONLY after their connection check passes (create and edit), and the
Nutanix source needs no NFS configuration (the worker mounts containers itself, read-only).

Usage: pytest tests/test_endpoint_validation.py
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from vmigrate.core.context import RunContext
from vmigrate.core.errors import ProviderError
from vmigrate.providers.nutanix.nfs import NfsMounter
from vmigrate.providers.nutanix.source import NutanixSource

from .test_platform import plat  # noqa: F401  (fixture)


# ---------------------------------------------------------------------- validate before save

def test_failing_connection_is_not_saved(plat):  # noqa: F811
    c, h = plat.c, plat.admin
    r = c.post("/endpoints", headers=h, json={"name": "bad", "type": "fake",
                                              "config": {"fail_connection": "host unreachable"},
                                              "credentials": {"password": "s3cret"}})
    assert r.status_code == 422
    d = r.json()["detail"]
    assert "not saved" in d["message"] and d["problems"] == ["host unreachable"]
    assert c.get("/endpoints", headers=h).json() == []
    assert plat.svc.secrets.status()["secrets"] == 0            # the secret was not stored either


def test_failing_edit_keeps_the_old_settings(plat):  # noqa: F811
    c, h = plat.c, plat.admin
    ep = c.post("/endpoints", headers=h, json={"name": "ok", "type": "fake", "config": {"vms": []},
                                               "credentials": {"password": "one"}}).json()
    r = c.patch(f"/endpoints/{ep['id']}", headers=h, json={"config": {"fail_connection": "bad token"}})
    assert r.status_code == 422 and r.json()["detail"]["problems"] == ["bad token"]
    assert c.get(f"/endpoints/{ep['id']}", headers=h).json()["config"] == {"vms": []}
    # credentials-only edit re-validates with the stored config and succeeds
    assert c.patch(f"/endpoints/{ep['id']}", headers=h, json={"credentials": {"password": "two"}}).status_code == 200
    assert plat.svc.secrets.get(plat.svc.repo.get_endpoint(ep["id"])["secret_id"]) == {"password": "two"}
    # description-only edits do not need a connection
    assert c.patch(f"/endpoints/{ep['id']}", headers=h, json={"description": "x"}).status_code == 200


def test_unreachable_nutanix_is_rejected(plat):  # noqa: F811
    r = plat.c.post("/endpoints", headers=plat.admin, json={
        "name": "ntnx", "type": "nutanix",
        "config": {"prism_element": "https://127.0.0.1:1", "username": "admin", "verify_tls": False,
                   "protection_domain": "pd1"},
        "credentials": {"password": "x"}})
    assert r.status_code == 422
    assert any("cannot reach Prism Element" in p for p in r.json()["detail"]["problems"])
    assert plat.c.get("/endpoints", headers=plat.admin).json() == []


# ---------------------------------------------------------------------- Nutanix automatic NFS

class Runner:
    def __init__(self, fail: str | None = None):
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if argv[0] == "mount" and self.fail:
            return SimpleNamespace(returncode=32, stdout="", stderr=self.fail)
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_mounter_mounts_read_only_reuses_and_cleans_up(tmp_path):
    run, mounted = Runner(), set()
    m = NfsMounter("10.0.0.10", base=str(tmp_path), run=run, mounted=lambda p: p in mounted)
    p = m.path_for("ctr-prod")
    assert p == tmp_path / "ctr-prod"
    argv = run.calls[0]
    assert argv[:3] == ["mount", "-t", "nfs"] and argv[4].startswith("ro,") and argv[5] == "10.0.0.10:/ctr-prod"
    mounted.add(str(tmp_path / "ctr-admin"))                      # mounted by someone else: reused, never unmounted
    assert m.path_for("ctr-admin") == tmp_path / "ctr-admin" and len(run.calls) == 1
    m.unmount_all()
    assert run.calls[-1] == ["umount", str(tmp_path / "ctr-prod")]
    assert not any(c[0] == "umount" and c[1].endswith("ctr-admin") for c in run.calls)
    with pytest.raises(ProviderError, match="unexpected container"):
        m.path_for("../etc")


def test_mounter_explains_whitelist_refusal(tmp_path):
    m = NfsMounter("127.0.0.1", base=str(tmp_path),
                   run=Runner(fail="mount.nfs: access denied by server while mounting 127.0.0.1:/ctr"),
                   mounted=lambda p: False)
    with pytest.raises(ProviderError, match=r"add \d+\.\d+\.\d+\.\d+ to the NFS whitelist of container 'ctr'"):
        m.path_for("ctr")


def test_nutanix_source_needs_no_nfs_config(tmp_path):
    run = Runner()
    m = NfsMounter("10.0.0.10", base=str(tmp_path), run=run, mounted=lambda p: False)
    src = NutanixSource("ahv", {"prism_element": "https://10.0.0.10:9440", "username": "admin"},
                        RunContext(dry_run=True, logger=logging.getLogger("t")), client=object(), mounter=m)
    assert src._local_path("/ctr-prod/.acropolis/vmdisk/abc") == tmp_path / "ctr-prod" / ".acropolis/vmdisk/abc"
    src.close()
    assert run.calls[-1][0] == "umount"
    # default host comes from the Prism Element URL
    src2 = NutanixSource("ahv", {"prism_element": "https://pe.example:9440", "username": "admin"},
                         RunContext(dry_run=True), client=object())
    assert src2.mounter.host == "pe.example"
    # explicit pre-mounted paths still win
    src3 = NutanixSource("ahv", {"prism_element": "https://pe:9440", "username": "a", "nfs_root": "/mnt/ntnx"},
                         RunContext(dry_run=True), client=object())
    assert str(src3._local_path("/c/x")) == "/mnt/ntnx/c/x"
