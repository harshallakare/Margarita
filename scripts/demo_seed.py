"""Purpose: seed a local demo platform with fake endpoints (no real infrastructure needed): a source
with the 13-disk RIS-Sainath VM, a source that reproduces Coriolis's 10-disk enumeration cap, and a
file-backed fake target. Also creates the demo admin user.

Usage: python scripts/demo_seed.py   (run by scripts/demo.sh; uses the VMIGRATE_* environment)
Demo login: admin / demo-admin-password  (local demo only; never reuse it anywhere else)
"""

from __future__ import annotations

import os

from vmigrate.platform.factory import Services
from vmigrate.platform.settings import Settings
from vmigrate.security.auth import hash_password

DEMO_USER, DEMO_PASSWORD = "admin", os.environ.get("DEMO_ADMIN_PASSWORD", "demo-admin-password")


def vm(name: str, disks: int, big: str = "64MiB", small: str = "32MiB") -> dict:
    return {"name": name, "os": "linux", "networks": ["vlan-120-prod", "vlan-300-backup"],
            "disks": [{"size": big if i == 0 else small, "boot": i == 0} for i in range(disks)]}


def main() -> None:
    svc = Services.from_settings(Settings.from_env())
    r = svc.repo
    if not r.get_user_by_name(DEMO_USER):
        r.create_user(DEMO_USER, hash_password(DEMO_PASSWORD), "admin")
    have = {e["name"] for e in r.list_endpoints("default")}
    data = svc.settings.data_dir
    if "ahv-demo" not in have:
        # cbt: simulated change tracking; raise `generation` (Edit endpoint) to simulate guest writes between syncs
        r.create_endpoint("default", "ahv-demo", "fake", {"vms": [vm("RIS-Sainath", 13), vm("web-01", 2), vm("db-02", 4)],
                                                          "cbt": True, "generation": 0},
                          svc.secrets.put({"password": "demo-not-a-real-secret"}), "Fake Nutanix AHV cluster")
    if "ahv-capped" not in have:
        r.create_endpoint("default", "ahv-capped", "fake",
                          {"vms": [vm("RIS-Sainath", 13)], "secondary_enumeration_cap": 10}, None,
                          "Reproduces the Coriolis 10-disk enumeration cap")
    if "vhi-demo" not in have:
        r.create_endpoint("default", "vhi-demo", "fake", {"staging_dir": str(data / "fake-target"), "persist": True},
                          svc.secrets.put({"password": "demo-not-a-real-secret"}), "Fake VHI target (files on disk)")
    print("demo seeded")


if __name__ == "__main__":
    main()
