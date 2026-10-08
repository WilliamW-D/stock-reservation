"""Transaction retry helper for transient concurrency errors (40001, 40P01)."""

from __future__ import annotations

import random
import time
from collections.abc import Callable

import psycopg

RETRYABLE_SQLSTATES = {
    "40001",  # serialization_failure
    "40P01",  # deadlock_detected
}


def execute_with_retry[T](
    operation: Callable[[], T],
    *,
    max_retries: int = 3,
    base_backoff_sec: float = 0.05,
    max_backoff_sec: float = 0.4,
) -> T:
    """Execute an operation with exponential backoff on deadlock or serialization conflicts.

    The operation MUST manage its own transaction so retries start completely fresh.
    """
    attempts = 0
    while True:
        try:
            return operation()
        except psycopg.Error as exc:
            sqlstate = getattr(exc.diag, "sqlstate", None) or getattr(exc, "sqlstate", None)
            if sqlstate in RETRYABLE_SQLSTATES and attempts < max_retries:
                attempts += 1
                backoff = min(max_backoff_sec, base_backoff_sec * (2 ** (attempts - 1)))
                jitter = random.uniform(0, backoff * 0.5)
                time.sleep(backoff + jitter)
                continue
            raise
