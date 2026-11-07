"""Purpose: VHI's Keystone connection; the implementation is shared with the OpenStack source and
lives in providers/common/openstack_auth.py (pitfall 7: project-ID scoping, env scrubbing).

Usage: from .auth import connect; conn = connect(cfg)
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..common.openstack_auth import SCRUB_ENV, scrub_env
from ..common.openstack_auth import auth_kwargs as _auth_kwargs
from ..common.openstack_auth import connect as _connect

__all__ = ["SCRUB_ENV", "auth_kwargs", "connect", "scrub_env"]


def auth_kwargs(cfg: Mapping[str, Any]) -> dict[str, Any]:
    return _auth_kwargs(cfg, "vhi target")


def connect(cfg: Mapping[str, Any]) -> Any:
    return _connect(cfg, "vhi target")
