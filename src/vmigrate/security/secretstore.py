"""Purpose: encrypted-at-rest secret storage for endpoint credentials (Barbican-equivalent). Secrets
are Fernet-encrypted (AES-128-CBC + HMAC-SHA256) with a key that lives OUTSIDE the database
(env var or key file), so a DB dump alone discloses nothing. The API never returns plaintext.

Usage:
    store = FernetSecretStore(db_engine, load_key())     # VMIGRATE_SECRET_KEY or VMIGRATE_SECRET_KEY_FILE
    sid = store.put({"password": "..."}); store.get(sid); store.delete(sid)
    vmigrate-platform gen-key                            # prints a new key

Key rotation: set VMIGRATE_SECRET_KEY to "new,old" (comma-separated). Reads try every key,
writes use the first one (MultiFernet).
Another backend (Barbican, Vault) implements the same SecretStore interface.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Engine

from ..core.errors import ConfigError
from ..db.schema import secrets as secrets_t


class SecretStore(ABC):
    @abstractmethod
    def put(self, data: dict[str, Any]) -> str: ...

    @abstractmethod
    def get(self, secret_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def replace(self, secret_id: str, data: dict[str, Any]) -> None: ...

    @abstractmethod
    def delete(self, secret_id: str) -> None: ...

    def status(self) -> dict[str, Any]:
        return {"backend": type(self).__name__}


def generate_key() -> str:
    return Fernet.generate_key().decode()


def load_key(env: dict[str, str] | None = None) -> str:
    env = dict(os.environ) if env is None else env
    key = env.get("VMIGRATE_SECRET_KEY", "").strip()
    if not key and env.get("VMIGRATE_SECRET_KEY_FILE"):
        key = Path(env["VMIGRATE_SECRET_KEY_FILE"]).read_text().strip()
    if not key:
        raise ConfigError("no secret key: set VMIGRATE_SECRET_KEY or VMIGRATE_SECRET_KEY_FILE "
                          "(generate one with `vmigrate-platform gen-key`)")
    return key


class FernetSecretStore(SecretStore):
    def __init__(self, engine: Engine, key: str):
        try:
            self._f = MultiFernet([Fernet(k.strip().encode()) for k in key.split(",") if k.strip()])
        except ValueError as e:
            raise ConfigError(f"invalid VMIGRATE_SECRET_KEY: {e}") from e
        self._nkeys = len([k for k in key.split(",") if k.strip()])
        self.engine = engine

    def _enc(self, data: dict[str, Any]) -> str:
        return self._f.encrypt(json.dumps(data, sort_keys=True).encode()).decode()

    def put(self, data: dict[str, Any]) -> str:
        sid = uuid.uuid4().hex
        with self.engine.begin() as c:
            c.execute(insert(secrets_t).values(id=sid, ciphertext=self._enc(data), created_at=time.time()))
        return sid

    def get(self, secret_id: str) -> dict[str, Any]:
        with self.engine.connect() as c:
            row = c.execute(select(secrets_t.c.ciphertext).where(secrets_t.c.id == secret_id)).first()
        if row is None:
            raise KeyError(secret_id)
        try:
            return json.loads(self._f.decrypt(row[0].encode()))
        except InvalidToken as e:
            raise ConfigError(f"secret {secret_id} cannot be decrypted with the configured key(s)") from e

    def replace(self, secret_id: str, data: dict[str, Any]) -> None:
        with self.engine.begin() as c:
            c.execute(secrets_t.update().where(secrets_t.c.id == secret_id).values(ciphertext=self._enc(data)))

    def delete(self, secret_id: str) -> None:
        with self.engine.begin() as c:
            c.execute(delete(secrets_t).where(secrets_t.c.id == secret_id))

    def status(self) -> dict[str, Any]:
        with self.engine.connect() as c:
            n = len(c.execute(select(secrets_t.c.id)).all())
        return {"backend": "fernet-db", "keys_configured": self._nkeys, "secrets": n}
