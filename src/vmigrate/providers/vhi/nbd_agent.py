"""Purpose: a minimal NBD server (fixed-newstyle handshake, simple replies) that serves ONE block
device on stdin/stdout. It runs on the VHI helper VM, started over SSH, so the worker can write,
read back and OSMorph a Cinder volume attached to the helper. Python standard library only, so any
cloud image with python3 works; no packages are installed on the helper.

Usage (on the helper, as root):
    python3 nbd_agent.py /dev/disk/by-id/virtio-<volume id prefix> rw|ro

The whole file is shipped as `python3 -c <source>`; keep it self-contained (no vmigrate imports).
Protocol: the NBD protocol specification (NetworkBlockDevice/nbd, doc/proto.md), reimplemented here.
Supported: OPT_EXPORT_NAME, OPT_INFO, OPT_GO, OPT_ABORT; CMD_READ, WRITE, DISC, FLUSH, WRITE_ZEROES.
"""

import os
import struct
import sys

NBDMAGIC = b"NBDMAGIC"
IHAVEOPT = 0x49484156454F5054
REPLY_MAGIC = 0x0003E889045565A9
REQ_MAGIC = 0x25609513
SIMPLE_REPLY = 0x67446698

FLAG_FIXED_NEWSTYLE, FLAG_NO_ZEROES = 1, 2
OPT_EXPORT_NAME, OPT_ABORT, OPT_INFO, OPT_GO = 1, 2, 6, 7
REP_ACK, REP_INFO, REP_ERR_UNSUP = 1, 3, 0x80000001
INFO_EXPORT = 0
T_HAS_FLAGS, T_READ_ONLY, T_SEND_FLUSH, T_SEND_FUA, T_SEND_WRITE_ZEROES = 1, 2, 4, 8, 64
CMD_READ, CMD_WRITE, CMD_DISC, CMD_FLUSH, CMD_WRITE_ZEROES = 0, 1, 2, 3, 6
CMD_FLAG_FUA = 1
EPERM, EIO, EINVAL = 1, 5, 22
ZERO_STEP = 4 << 20


def _read_exact(fd, n):
    buf = bytearray()
    while len(buf) < n:
        b = os.read(fd, n - len(buf))
        if not b:
            raise EOFError("peer closed the connection")
        buf += b
    return bytes(buf)


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def _pwrite_all(dev, data, off):
    view = memoryview(data)
    while view:
        n = os.pwrite(dev, view, off)
        view, off = view[n:], off + n


def _pread_exact(dev, n, off):
    out = bytearray()
    while len(out) < n:
        b = os.pread(dev, n - len(out), off + len(out))
        if not b:
            out += bytes(n - len(out))      # past EOF reads as zero (never expected inside size)
            break
        out += b
    return bytes(out)


def serve(dev, size, writable, rfd=0, wfd=1):
    tflags = T_HAS_FLAGS | T_SEND_FLUSH | T_SEND_FUA | T_SEND_WRITE_ZEROES | (0 if writable else T_READ_ONLY)
    _write_all(wfd, NBDMAGIC + struct.pack(">QH", IHAVEOPT, FLAG_FIXED_NEWSTYLE | FLAG_NO_ZEROES))
    (cflags,) = struct.unpack(">I", _read_exact(rfd, 4))
    no_zeroes = bool(cflags & FLAG_NO_ZEROES)

    def reply(opt, rtype, data=b""):
        _write_all(wfd, struct.pack(">QIII", REPLY_MAGIC, opt, rtype, len(data)) + data)

    while True:                                     # option haggling
        magic, opt, ln = struct.unpack(">QII", _read_exact(rfd, 16))
        if magic != IHAVEOPT:
            raise ValueError("bad option magic")
        _read_exact(rfd, ln)                        # export name / info requests: one export, ignored
        if opt == OPT_EXPORT_NAME:
            _write_all(wfd, struct.pack(">QH", size, tflags) + (b"" if no_zeroes else bytes(124)))
            break
        if opt in (OPT_INFO, OPT_GO):
            reply(opt, REP_INFO, struct.pack(">HQH", INFO_EXPORT, size, tflags))
            reply(opt, REP_ACK)
            if opt == OPT_GO:
                break
            continue
        if opt == OPT_ABORT:
            reply(opt, REP_ACK)
            return
        reply(opt, REP_ERR_UNSUP)

    while True:                                     # transmission
        magic, flags, ctype, handle, off, ln = struct.unpack(">IHHQQI", _read_exact(rfd, 28))
        if magic != REQ_MAGIC:
            raise ValueError("bad request magic")
        data = _read_exact(rfd, ln) if ctype == CMD_WRITE else b""
        if ctype == CMD_DISC:
            if writable:
                os.fsync(dev)
            return
        err, payload = 0, b""
        try:
            if ctype in (CMD_READ, CMD_WRITE, CMD_WRITE_ZEROES) and off + ln > size:
                err = EINVAL
            elif ctype == CMD_READ:
                payload = _pread_exact(dev, ln, off)
            elif ctype in (CMD_WRITE, CMD_WRITE_ZEROES):
                if not writable:
                    err = EPERM
                else:
                    if ctype == CMD_WRITE:
                        _pwrite_all(dev, data, off)
                    else:
                        zero, pos = bytes(min(ZERO_STEP, ln)), off
                        while pos < off + ln:
                            n = min(ZERO_STEP, off + ln - pos)
                            _pwrite_all(dev, zero[:n], pos)
                            pos += n
                    if flags & CMD_FLAG_FUA:
                        os.fsync(dev)
            elif ctype == CMD_FLUSH:
                if writable:
                    os.fsync(dev)
            else:
                err = EINVAL
        except OSError:
            err, payload = EIO, b""
        _write_all(wfd, struct.pack(">IIQ", SIMPLE_REPLY, err, handle) + (payload if not err else b""))


def main(argv):
    path, mode = argv[1], (argv[2] if len(argv) > 2 else "ro")
    writable = mode == "rw"
    dev = os.open(path, os.O_RDWR if writable else os.O_RDONLY)
    try:
        size = os.lseek(dev, 0, os.SEEK_END)
        serve(dev, size, writable)
    except EOFError:
        pass
    finally:
        if writable:
            try:
                os.fsync(dev)
            except OSError:
                pass
        os.close(dev)


if __name__ == "__main__":
    main(sys.argv)
