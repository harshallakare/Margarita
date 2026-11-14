"""Purpose: generic file-backed DiskReader/DiskWriter used by many adapters (a source that can only
produce a whole raw file satisfies DiskReader by wrapping it; targets stage raw images locally).

Usage:
    with RawFileReader(path, read_block=64 << 20) as r: r.read(0, 1 << 20)
    with RawFileWriter(path, size, create=True) as w: w.write(off, buf)

RawFileReader reads strictly linearly-friendly (plain pread, large blocks) and NEVER hole-probes
(SEEK_DATA/SEEK_HOLE): hole probing hangs on Nutanix NFS vdisks (pitfall 3).
RawFileWriter creates a sparse file (truncate to size) so it is `prezeroed`.
"""

from __future__ import annotations

import os
from pathlib import Path

from .interfaces import DiskReader, DiskWriter


class RawFileReader(DiskReader):
    def __init__(self, path: str | os.PathLike[str], size: int | None = None):
        self.path = Path(path)
        self._fd = os.open(self.path, os.O_RDONLY)
        self._size = size if size is not None else os.fstat(self._fd).st_size

    @property
    def size(self) -> int:
        return self._size

    def read(self, offset: int, length: int) -> bytes:
        length = max(0, min(length, self._size - offset))
        chunks = []
        got = 0
        while got < length:
            b = os.pread(self._fd, length - got, offset + got)
            if not b:
                break
            chunks.append(b)
            got += len(b)
        data = b"".join(chunks)
        if len(data) < length:
            # Source file shorter than the declared disk size: tail reads as zeros.
            data += bytes(length - len(data))
        return data

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


class RawFileWriter(DiskWriter):
    def __init__(self, path: str | os.PathLike[str], size: int, *, create: bool = True):
        self.path = Path(path)
        self._size = size
        flags = os.O_RDWR | (os.O_CREAT if create else 0)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        self._fd = os.open(self.path, flags, 0o600)
        cur = os.fstat(self._fd).st_size
        if cur < size:
            os.ftruncate(self._fd, size)  # sparse extension -> unwritten bytes read as zero
        elif cur > size:
            raise ValueError(f"{self.path} is larger ({cur}) than target size {size}")
        # A brand-new sparse file reads as zeros. An existing file being resumed is also safe:
        # the copier only skips zero chunks beyond the watermark, i.e. never-written regions.
        self.prezeroed = True
        self._existed = existed

    @property
    def size(self) -> int:
        return self._size

    def write(self, offset: int, buf: bytes) -> None:
        if offset + len(buf) > self._size:
            raise ValueError(f"write past end: {offset}+{len(buf)} > {self._size}")
        view = memoryview(buf)
        done = 0
        while done < len(buf):
            done += os.pwrite(self._fd, view[done:], offset + done)

    def flush(self) -> None:
        if self._fd >= 0:
            os.fsync(self._fd)

    def close(self) -> None:
        if self._fd >= 0:
            self.flush()
            os.close(self._fd)
            self._fd = -1

    @property
    def staged_path(self) -> Path:
        return self.path


class BlockDeviceWriter(DiskWriter):
    """Writes into an existing block device (an iSCSI LUN, a mapped volume). Never truncates; the device
    must be at least `size` bytes. `prezeroed` is the caller's knowledge (a fresh thin LUN reads as zeros).
    staged_path is None: the data is already on the target, nothing is converted afterwards."""

    def __init__(self, path: str | os.PathLike[str], size: int, *, prezeroed: bool):
        self.path = Path(path)
        self._size = size
        self._fd = os.open(self.path, os.O_RDWR)
        dev = os.lseek(self._fd, 0, os.SEEK_END)
        if dev < size:
            os.close(self._fd)
            raise ValueError(f"{self.path} is {dev} bytes, smaller than the disk ({size})")
        self.prezeroed = prezeroed

    @property
    def size(self) -> int:
        return self._size

    def write(self, offset: int, buf: bytes) -> None:
        if offset + len(buf) > self._size:
            raise ValueError(f"write past end: {offset}+{len(buf)} > {self._size}")
        view = memoryview(buf)
        done = 0
        while done < len(buf):
            done += os.pwrite(self._fd, view[done:], offset + done)

    def flush(self) -> None:
        if self._fd >= 0:
            os.fsync(self._fd)

    def close(self) -> None:
        if self._fd >= 0:
            self.flush()
            os.close(self._fd)
            self._fd = -1
