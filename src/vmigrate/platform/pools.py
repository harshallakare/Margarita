"""Purpose: worker pools ("minion pools") — named groups of workers that endpoints or migrations can
require, with optional auto-provisioned minion VMs on an OpenStack-family cloud (VHI, OpenStack).

Usage:
    members(pool, workers, alive_after)          # live workers tagged pool:<name>
    scale(repo, pool, provisioner, now)          # conductor: create minions up to min_size
    user_data(api_url, token, repo, branch)      # cloud-init for a new minion

Membership: a worker belongs to pool P when it has the tag `pool:P` (deploy.sh --pool P, or the
enrollment response). Routing then uses the existing capability matching: a migration with
options.pool = P (or an endpoint whose required_tags include pool:P) only runs on P's workers.

Enrollment: a new worker joins with a single-use, short-lived token (POST /enroll) and receives
the database URL and secret key over HTTPS. Minion user-data therefore only carries that token,
never the secret key itself.

Auto-provisioning (opt-in, pool.provision set): the conductor keeps at least min_size members
(live workers + minions still booting) and never exceeds max_size minions. It never deletes
minions on its own; DELETE /pools/{id}/minions/{server_id} does (admin).
"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import Mapping
from typing import Any

log = logging.getLogger("vmigrate.pools")

BOOT_GRACE_S = 20 * 60          # a minion that has not joined after this is reported as stuck
DEFAULT_REPO = "https://github.com/harshallakare/Margarita.git"


def tag(pool_name: str) -> str:
    return f"pool:{pool_name}"


def members(pool: Mapping[str, Any], workers: list[Mapping[str, Any]], alive_after: float) -> list[dict[str, Any]]:
    """A member is a worker host: an autoscaling supervisor or a single worker. A supervisor's per-job
    processes are not counted separately (they come and go with the jobs)."""
    t = tag(pool["name"])

    def busy(w: Mapping[str, Any]) -> bool:
        info = w.get("info") or {}
        return int(info.get("running") or 0) > 0 if info.get("role") == "supervisor" else bool(w.get("current_execution"))
    return [{"id": w["id"], "hostname": w.get("hostname"), "region": w.get("region"),
             "busy": busy(w), "heartbeat_at": w.get("heartbeat_at")}
            for w in workers if t in (w.get("capabilities") or []) and (w.get("heartbeat_at") or 0) >= alive_after
            and "export-agent" not in (w.get("capabilities") or []) and (w.get("info") or {}).get("role") != "job"]


def user_data(api_url: str, token: str, *, repo: str = DEFAULT_REPO, branch: str = "main", full: bool = False) -> str:
    raw = repo.replace(".git", "").replace("https://github.com/", "https://raw.githubusercontent.com/")
    return f"""#!/bin/sh
# Margarita minion: install a worker and join the pool with a single-use enrollment token.
set -e
curl -fsSL {raw}/{branch}/scripts/deploy.sh -o /root/margarita-deploy.sh
bash /root/margarita-deploy.sh --repo {repo} --branch {branch} --enroll {api_url} --enroll-token {token}{' --full' if full else ''}
rm -f /root/margarita-deploy.sh
"""


class OpenStackMinions:
    """Creates/deletes minion servers through openstacksdk (VHI and OpenStack endpoints)."""

    def __init__(self, provider_config: Mapping[str, Any]):
        from ..providers.common.openstack_auth import connect
        self.conn = connect(provider_config, "minion pool")

    def create(self, name: str, *, image: str, flavor: str, network: str, user_data: str,
               key_name: str | None = None, security_groups: list[str] | None = None) -> str:
        img = self.conn.image.find_image(image, ignore_missing=False)
        flv = self.conn.compute.find_flavor(flavor, ignore_missing=False)
        net = self.conn.network.find_network(network, ignore_missing=False)
        kw: dict[str, Any] = {"name": name, "image_id": img.id, "flavor_id": flv.id, "networks": [{"uuid": net.id}],
                              "user_data": base64.b64encode(user_data.encode()).decode(),
                              "metadata": {"margarita": "minion"}}
        if key_name:
            kw["key_name"] = key_name
        if security_groups:
            kw["security_groups"] = [{"name": g} for g in security_groups]
        return str(self.conn.compute.create_server(**kw).id)

    def status(self, server_id: str) -> str:
        s = self.conn.compute.find_server(server_id)
        return str(s.status).lower() if s is not None else "gone"

    def delete(self, server_id: str) -> None:
        s = self.conn.compute.find_server(server_id)
        if s is None:
            return
        if (s.metadata or {}).get("margarita") != "minion":
            raise PermissionError(f"server {server_id} is not a Margarita minion; refusing to delete it")
        self.conn.compute.delete_server(server_id)


def scale(repo: Any, pool: dict[str, Any], provisioner: Any, *, alive_after: float, now: float | None = None) -> list[str]:
    """Create minions so that live members + booting minions >= min_size (capped by max_size).
    Returns the new server ids. Records state on the pool row."""
    now = now or time.time()
    prov = pool.get("provision") or {}
    st = dict(pool.get("state") or {})
    minions = list(st.get("minions") or [])
    live = members(pool, repo.list_workers(), alive_after)
    live_hosts = {m["hostname"] for m in live}
    for m in minions:                                  # mark joined / stuck
        if m["name"] in live_hosts:
            m["status"] = "joined"
        elif m.get("status") == "booting" and now - m["created_at"] > BOOT_GRACE_S:
            m["status"] = "stuck"
    booting = sum(1 for m in minions if m.get("status") == "booting")
    want = int(pool.get("min_size") or 0) - len(live) - booting
    room = int(pool.get("max_size") or 0) - sum(1 for m in minions if m.get("status") != "deleted")
    created: list[str] = []
    for _ in range(max(0, min(want, room))):
        name = f"margarita-minion-{pool['name']}-{int(now)}-{len(minions)}"
        token, _exp = repo.create_enroll_token(pool["id"], 2 * 3600, None)
        try:
            sid = provisioner.create(name, image=prov["image"], flavor=prov["flavor"], network=prov["network"],
                                     user_data=user_data(prov["api_url"], token, repo=prov.get("repo") or DEFAULT_REPO,
                                                         branch=prov.get("branch") or "main", full=bool(prov.get("full"))),
                                     key_name=prov.get("key_name"), security_groups=prov.get("security_groups"))
        except Exception as e:  # noqa: BLE001 - recorded on the pool; retried next tick
            st["last_error"] = f"{type(e).__name__}: {e}"
            log.warning("pool %s: minion create failed: %s", pool["name"], e)
            break
        minions.append({"server_id": sid, "name": name, "created_at": now, "status": "booting"})
        created.append(sid)
        st.pop("last_error", None)
    st["minions"], st["last_scale_at"] = minions, now
    repo.update_pool(pool["id"], state=st)
    return created
