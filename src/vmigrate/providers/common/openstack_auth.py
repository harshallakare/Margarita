"""Purpose: OpenStack (Keystone) connection with project-ID scoping that survives a non-default
Keystone domain (pitfall 7). Shared by the VHI target and the OpenStack source.

Usage:
    conn = connect(cfg)       # cfg: auth_url, username, password, user_domain_name|user_domain_id,
                              #      project_id  OR  project_name + project_domain_name|project_domain_id,
                              #      region_name?, verify_tls?

Why: a stray OS_PROJECT_NAME / OS_PROJECT_DOMAIN_NAME / OS_PROJECT_DOMAIN_ID in the environment
combined with a project in a non-default domain produces a 401. We (1) scrub those variables from
this process's environment (so subprocesses don't see them either), (2) disable env/clouds.yaml
loading, and (3) scope by project ID, or by project name WITH an explicit project domain (the way
Coriolis endpoints are configured; the domain defaults to the user's domain when not given).
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
    for k in ("auth_url", "username"):
        if not cfg.get(k):
            raise ConfigError(f"{who}: missing {k!r}")
    if not cfg.get("project_id") and not cfg.get("project_name"):
        raise ConfigError(f"{who}: set project_id, or project_name + project_domain_name")
    kw: dict[str, Any] = {
        "auth_url": cfg["auth_url"],
        "username": cfg["username"],
        "password": cfg.get("password") or os.environ.get(cfg.get("password_env", ""), ""),
        "project_id": cfg.get("project_id") or None,
        "region_name": cfg.get("region_name"),
        "verify": cfg.get("verify_tls", True),
        "load_envvars": False,
        "load_yaml_config": False,
        "identity_interface": cfg.get("interface", "public"),
        # retry dropped/reset connections (seen live: Nova/Neutron "Remote end closed connection
        # without response" on reused keep-alive connections) and 503s, with backoff
        "connect_retries": int(cfg.get("connect_retries", 5)),
        "status_code_retries": int(cfg.get("status_code_retries", 3)),
    }
    if cfg.get("user_domain_id"):
        kw["user_domain_id"] = cfg["user_domain_id"]
    else:
        kw["user_domain_name"] = cfg.get("user_domain_name") or "Default"
    if not cfg.get("project_id"):                  # by name: always with an explicit project domain
        kw["project_name"] = cfg["project_name"]
        if cfg.get("project_domain_id"):
            kw["project_domain_id"] = cfg["project_domain_id"]
        elif cfg.get("project_domain_name"):
            kw["project_domain_name"] = cfg["project_domain_name"]
        elif "user_domain_name" in kw:             # same domain as the user (the usual VHI setup)
            kw["project_domain_name"] = kw["user_domain_name"]
        else:
            raise ConfigError(f"{who}: project_name needs project_domain_name (or project_domain_id)")
    return {k: v for k, v in kw.items() if v is not None}


def project_problem(pid: str, cfg: Mapping[str, Any]) -> list[str]:
    """The token must be scoped to the configured project (by ID when one is configured)."""
    want = cfg.get("project_id")
    if want and pid != want:
        return [f"token scoped to project {pid}, expected {want} (grant the user a role on the target project)"]
    return []


def connect(cfg: Mapping[str, Any], who: str = "openstack") -> Any:
    import openstack  # SDK imported lazily: only OpenStack-family adapters reach this

    scrub_env()
    return openstack.connect(**auth_kwargs(cfg, who))
