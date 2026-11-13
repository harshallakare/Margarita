"""Purpose: decide which worker may run which execution (regions + capabilities), so a job only goes
to a worker that can actually do it — e.g. a VHI-target migration only to a worker on a VHI node
(qemu-img, root, vStorage), a Nutanix-iSCSI read only where iscsiadm exists, and with several
sites, only to a worker in the right region. Same idea as Coriolis' scheduler filters
(regions / provider types / enabled), re-implemented.

Usage:
    caps = detect_capabilities(env)                      # on the worker, at start
    req = requirements(src_endpoint, tgt_endpoint)       # conductor, when queuing
    ok, why = can_run(worker_region, caps, req)          # claim time / "waiting" explanation

Capability strings:
    provider:<type>    the provider's SDK is importable here (vmware -> pyVmomi, aws -> boto3, ...)
    tool:<name>        a tool on PATH: qemu-img, iscsiadm, nbdkit, guestfish, mount.nfs, ssh,
                       virt-v2v-in-place, rbd, qemu-nbd
    vstorage           a VHI vStorage mount is present (/mnt/vstorage or VMIGRATE_VSTORAGE_MOUNT)
    root               the worker runs as root
    <anything>         operator tags from VMIGRATE_WORKER_TAGS (comma-separated), e.g. site-a, gpu
Endpoints may add `required_tags`; providers add inherent needs (PROVIDER_NEEDS).
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

SDK = {"vmware": "pyVmomi", "hyperv": "winrm", "openstack": "openstack", "vhi": "openstack", "aws": "boto3",
       "azure": None, "libvirt": None, "ovirt": None, "xen": None, "gcp": "cryptography", "oci": "cryptography",
       "nutanix": "requests", "proxmox": "requests", "fake": None, "physical": None}
TOOLS = ("qemu-img", "iscsiadm", "nbdkit", "guestfish", "mount.nfs", "ssh", "virt-v2v-in-place", "rbd", "qemu-nbd")
# inherent per-role needs: (provider type, role) -> capabilities
PROVIDER_NEEDS: dict[tuple[str, str], set[str]] = {
    ("vhi", "target"): {"tool:qemu-img", "root"},          # writes VMDS qcow2 backings on the node
    ("physical", "source"): {"tool:ssh"},                   # disks stream over ssh pipes
    ("proxmox", "source"): {"tool:ssh"},
    ("libvirt", "source"): {"tool:ssh"},                    # virsh + NBD over ssh                    # qemu-nbd on the node, socket forwarded over ssh
}


def detect_capabilities(env: Mapping[str, str] | None = None, *, which: Callable[[str], Any] = shutil.which,
                        has_module: Callable[[str], bool] | None = None, is_dir: Callable[[str], bool] = os.path.isdir,
                        euid: int | None = None) -> set[str]:
    env = dict(os.environ) if env is None else env
    has_module = has_module or (lambda m: importlib.util.find_spec(m) is not None)
    caps: set[str] = set()
    for ptype, mod in SDK.items():
        if mod is None or has_module(mod):
            caps.add(f"provider:{ptype}")
    caps |= {f"tool:{t}" for t in TOOLS if which(t)}
    if is_dir(env.get("VMIGRATE_VSTORAGE_MOUNT", "/mnt/vstorage")):
        caps.add("vstorage")
    if (os.geteuid() if euid is None else euid) == 0:
        caps.add("root")
    caps |= {t.strip() for t in env.get("VMIGRATE_WORKER_TAGS", "").split(",") if t.strip()}
    return caps


@dataclass
class Requirement:
    region: str | None = None
    caps: set[str] = field(default_factory=set)
    cross_site: bool = False             # source and target endpoints are in different regions
    source_region: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"region": self.region, "caps": sorted(self.caps), "cross_site": self.cross_site,
                "source_region": self.source_region}


def requirements(src: Mapping[str, Any], tgt: Mapping[str, Any]) -> Requirement:
    """Execution requirements from its two endpoint rows. The worker runs where the TARGET is
    (writes need locality: VHI backings, local staging); with a cross-site pair the source disks are
    served by a worker in the source region (remote export)."""
    caps = {f"provider:{src['type']}", f"provider:{tgt['type']}"}
    caps |= PROVIDER_NEEDS.get((src["type"], "source"), set()) | PROVIDER_NEEDS.get((tgt["type"], "target"), set())
    caps |= set(src.get("required_tags") or []) | set(tgt.get("required_tags") or [])
    sr, tr = src.get("region") or None, tgt.get("region") or None
    cross = bool(sr and tr and sr != tr)
    if cross:   # source-side tags are satisfied by the exporting worker, not this one
        caps -= set(src.get("required_tags") or [])
        caps.discard(f"provider:{src['type']}")
    return Requirement(region=tr or sr, caps=caps, cross_site=cross, source_region=sr if cross else None)


def can_run(worker_region: str | None, worker_caps: set[str] | None, req: Mapping[str, Any] | None) -> tuple[bool, str]:
    if not req:
        return True, "no requirements (legacy execution)"
    region = req.get("region")
    if region and (worker_region or None) != region:
        return False, f"needs region {region!r} (worker is in {worker_region or 'no region'!r})"
    need = set(req.get("caps") or [])
    if worker_caps is None:
        return (not need), "worker did not report capabilities"
    missing = sorted(need - set(worker_caps))
    if missing:
        return False, f"missing capabilities {missing}"
    return True, "ok"


def pick_export_agent(workers: list[Mapping[str, Any]], region: str, provider_type: str,
                      alive_after: float) -> Mapping[str, Any] | None:
    """Freshest live export agent in `region` that can run `provider_type`."""
    ok = [w for w in workers if (w.get("heartbeat_at") or 0) >= alive_after and w.get("export_url")
          and w.get("region") == region and {"export-agent", f"provider:{provider_type}"} <= set(w.get("capabilities") or [])]
    return max(ok, key=lambda w: w.get("heartbeat_at") or 0) if ok else None


def waiting_reason(workers: list[Mapping[str, Any]], req: Mapping[str, Any] | None, alive_after: float) -> str | None:
    """None when at least one live worker can run it; else a human explanation."""
    live = [w for w in workers if (w.get("heartbeat_at") or 0) >= alive_after
            and "export-agent" not in (w.get("capabilities") or [])]      # agents serve disks, never claim jobs
    if not live:
        return "no live worker (start one: vmigrate-platform worker)"
    reasons = []
    for w in live:
        ok, why = can_run(w.get("region"), set(w.get("capabilities") or []) if w.get("capabilities") is not None
                          else None, req)
        if ok:
            return None
        reasons.append(f"{w.get('id')}: {why}")
    return "no live worker can run this — " + "; ".join(reasons[:5])
