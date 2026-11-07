"""Purpose: retry-with-exponential-backoff helper for provider API calls, plus a poll-until helper.

Usage:
    result = retry(lambda: client.get(url), attempts=5, retry_on=(TransientProviderError,))
    vol = poll_until(lambda: conn.get_volume(id), lambda v: v.status == "available", timeout=600)

Safety gates (SafetyGateError) are never retried, whatever retry_on says.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

from .errors import ProviderError, SafetyGateError, TransientProviderError

T = TypeVar("T")
log = logging.getLogger("vmigrate.retry")


def retry(
    fn: Callable[[], T],
    *,
    attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    retry_on: tuple[type[BaseException], ...] = (TransientProviderError,),
    sleep: Callable[[float], None] = time.sleep,
    what: str = "operation",
) -> T:
    last: BaseException | None = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except SafetyGateError:
            raise
        except retry_on as e:  # type: ignore[misc]
            last = e
            if i == attempts:
                break
            delay = min(max_delay, base_delay * 2 ** (i - 1)) * (0.5 + random.random() / 2)
            log.warning("%s failed (attempt %d/%d): %s; retrying in %.1fs", what, i, attempts, e, delay)
            sleep(delay)
    assert last is not None
    raise last


def poll_until(
    get: Callable[[], T],
    done: Callable[[T], bool],
    *,
    timeout: float,
    interval: float = 5.0,
    failed: Callable[[T], bool] | None = None,
    what: str = "condition",
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> T:
    deadline = clock() + timeout
    while True:
        cur = get()
        if done(cur):
            return cur
        if failed is not None and failed(cur):
            raise ProviderError(f"{what}: entered failed state: {cur!r}")
        if clock() >= deadline:
            raise ProviderError(f"{what}: timed out after {timeout}s (last={cur!r})")
        sleep(interval)
