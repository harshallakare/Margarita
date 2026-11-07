"""Purpose: local identity + RBAC. Passwords are scrypt-hashed; API tokens are random 256-bit
bearer tokens stored only as SHA-256 hashes. Roles are ordered viewer < operator < admin.
OIDC / Keystone plug in later as alternative `authenticate` paths that map to the same Principal.

Usage:
    h = hash_password("pw"); verify_password("pw", h)
    principal.require("operator")       # raises PermissionError when the role is too low

Role meanings:
    viewer   read everything in the project (endpoints without secrets, migrations, executions)
    operator viewer + create/update/delete endpoints and migrations, start/cancel/retry executions
    admin    operator + manage users, see every project
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass

ROLES = ("viewer", "operator", "admin")
_N, _R, _P = 2**14, 8, 1


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    calc = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
    return hmac.compare_digest(calc, base64.b64decode(dk))


def new_token() -> tuple[str, str]:
    """(plaintext token for the client, hash to store)."""
    t = secrets.token_urlsafe(32)
    return t, token_hash(t)


def token_hash(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    role: str
    project: str

    def has(self, role: str) -> bool:
        return ROLES.index(self.role) >= ROLES.index(role)

    def require(self, role: str) -> None:
        if not self.has(role):
            raise PermissionError(f"role {self.role!r} cannot do this (needs {role!r})")

    def sees_project(self, project: str) -> bool:
        return self.role == "admin" or project == self.project
