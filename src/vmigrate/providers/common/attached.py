"""Purpose: helpers for sources that read a temporary cloud disk attached to the worker VM (GCP,
OCI): the block device size, and a reference-counted view so copy and verify can share one
attachment (the owner detaches and deletes the disk when the last view closes).

Usage:
    r = SharedView(owner, key, raw_reader)     # owner._release(key) is called once on close
    block_size("/dev/disk/by-id/google-x")
"""

from __future__ import annotations

import os
from typing import Any

from ...core.interfaces import DiskReader


def block_size(path: str) -> int:
    fd = os.open(path, os.O_RDONLY)
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)


class SharedView(DiskReader):
    def __init__(self, owner: Any, key: str, reader: DiskReader):
        self.owner, self.key, self.r = owner, key, reader

    @property
    def size(self) -> int:
        return self.r.size

    def read(self, offset: int, length: int) -> bytes:
        return self.r.read(offset, length)

    def close(self) -> None:
        if self.owner is not None:
            owner, self.owner = self.owner, None
            owner._release(self.key)
