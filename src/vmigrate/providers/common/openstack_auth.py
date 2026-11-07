"""Purpose: OpenStack (Keystone) connection with project-ID scoping that survives a non-default
Keystone domain (pitfall 7). Shared by the VHI target and the OpenStack source.

Usage:
    conn = connect(cfg)       # cfg: auth_url, username, password, user_domain_name|user_domain_id,
                              #      project_id, region_name?, verify_tls?

Why: a stray OS_PROJECT_NAME / OS_PROJECT_DOMAIN_NAME / OS_PROJECT_DOMAIN_ID in the environment
combined with a project in a non-default domain produces a 401. We (1) scrub those variables from
this process's environment (so subprocesses don't see them either), (2) disable env/clouds.yaml
loading, and (3) scope strictly by project ID.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from ...core.errors import ConfigError

SCRUB_ENV = ("OS_PROJECT_NAME", "OS_PROJECT_DOMAIN_NAME", "OS_PROJECT_DOMAIN_ID",
             "OS_TENANT_NAME", "OS_TENANT_ID", "OS_CLOUD")


def scrub_env(env: dict[str, str] | None = None) -> list[str]:
    e = os.environ if env is None else env
    removed = [k for k in SCRUB_ENV if k in e]
    for k in removed:
        del e[k]
    return removed


def auth_kwargs(cfg: Mapping[str, Any], who: str = "openstack") -> dict[str, Any]:
    for k in ("auth_url", "username", "project_id"):
        if not cfg.get(k):
            raise ConfigError(f"{who}: missing {k!r} (scope by project_id, not name)")
    if cfg.get("project_name"):
        raise ConfigError(f"{who}: use project_id, not project_name (non-default Keystone domain -> 401)")
    kw: dict[str, Any] = {
        "auth_url": cfg["auth_url"],
        "username": cfg["username"],
        "password": cfg.get("password") or os.environ.get(cfg.get("password_env", ""), ""),
        "project_id": cfg["project_id"],
        "region_name": cfg.get("region_name"),
        "verify": cfg.get("verify_tls", True),
        "load_envvars": False,
        "load_yaml_config": False,
        "identity_interface": cfg.get("interface", "public"),
    }
    if cfg.get("user_domain_id"):
        kw["user_domain_id"] = cfg["user_domain_id"]
    else:
        kw["user_domain_name"] = cfg.get("user_domain_name", "Default")
    return {k: v for k, v in kw.items() if v is not None}


def connect(cfg: Mapping[str, Any], who: str = "openstack") -> Any:
    import openstack  # SDK imported lazily: only OpenStack-family adapters reach this

    scrub_env()
    return openstack.connect(**auth_kwargs(cfg, who))
