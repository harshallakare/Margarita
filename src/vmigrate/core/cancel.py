"""Purpose: cooperative cancellation for the thread that runs a task. The task engine binds the
execution's cancel check to the task's thread; long waits and external commands made from that
thread then stop within about a second of a cancel instead of running to their own timeout
(seen live: a cancel during the 15-minute boot check sat in "canceling" until the check ended).

Usage:
    with cancel.bound(dag.cancelled):        # done by the DAG runner around each task
        cancel.sleep(10)                     # returns early by raising OperationCanceled
        cancel.run(["guestfish", ...], input=script, capture_output=True, text=True, timeout=1800)
    cancel.check()                           # raise now if the execution was canceled

Outside a bound thread (CLI, tests, helper threads) everything behaves like time.sleep /
subprocess.run.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from .errors import OperationCanceled

_local = threading.local()
STEP = 1.0          # how often a wait or a command looks at the cancel flag


def current() -> Callable[[], bool] | None:
    return getattr(_local, "check", None)


@contextmanager
def bound(check: Callable[[], bool] | None) -> Iterator[None]:
    prev = current()
    _local.check = check
    try:
        yield
    finally:
        _local.check = prev


def canceled() -> bool:
    c = current()
    return bool(c and c())


def check(what: str = "operation") -> None:
    if canceled():
        raise OperationCanceled(f"{what} canceled")


def sleep(seconds: float) -> None:
    """time.sleep that ends early (OperationCanceled) when the execution is canceled."""
    c = current()
    if c is None:
        time.sleep(seconds)
        return
    end = time.monotonic() + max(0.0, seconds)
    while True:
        if c():
            raise OperationCanceled("wait canceled")
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(STEP, left))


def run(args: Any, *, input: Any = None, timeout: float | None = None,  # noqa: A002 - mirrors subprocess.run
        capture_output: bool = False, **kw: Any) -> subprocess.CompletedProcess:
    """subprocess.run that kills the command when the execution is canceled."""
    c = current()
    if c is None:
        return subprocess.run(args, input=input, timeout=timeout, capture_output=capture_output, **kw)
    if capture_output:
        kw["stdout"] = kw["stderr"] = subprocess.PIPE
    if input is not None:
        kw["stdin"] = subprocess.PIPE
    deadline = None if timeout is None else time.monotonic() + timeout
    with subprocess.Popen(args, **kw) as p:
        first = True
        while True:
            try:
                out, err = p.communicate(input if first else None, timeout=STEP)
                break
            except subprocess.TimeoutExpired:
                first = False
                if c():
                    p.kill()
                    p.communicate()
                    raise OperationCanceled(f"command canceled: {_name(args)}") from None
                if deadline is not None and time.monotonic() >= deadline:
                    p.kill()
                    out, err = p.communicate()
                    raise subprocess.TimeoutExpired(args, timeout or 0, out, err) from None
        return subprocess.CompletedProcess(args, p.returncode, out, err)


def _name(args: Any) -> str:
    return str(args[0] if isinstance(args, (list, tuple)) and args else args)[:80]
