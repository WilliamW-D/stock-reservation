"""Tools for making concurrent requests *actually* overlap.

Two strategies are used in the tests:

1. ``run_concurrently`` — every worker gets its own connection (opened before the
   start line), then all workers are released together by a ``threading.Barrier``.
   Repeated many times, this produces real contention.

2. ``wait_until_blocked`` — deterministic interleaving. Transaction A does its work
   and *holds* its locks; we start B and poll ``pg_stat_activity`` until PostgreSQL
   reports B is waiting on a lock. Only then does A commit. This proves the two
   transactions overlapped instead of hoping they did.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

import psycopg


@dataclass
class Outcome:
    worker: int
    value: Any = None
    error: BaseException | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def run_concurrently(
    connections: list[psycopg.Connection], fn: Callable[[psycopg.Connection, int], Any]
) -> list[Outcome]:
    barrier = threading.Barrier(len(connections))

    def worker(index: int) -> Outcome:
        conn = connections[index]
        barrier.wait()  # every worker is connected and ready; release them together
        try:
            return Outcome(index, value=fn(conn, index))
        except Exception as exc:  # captured and asserted on by the test
            return Outcome(index, error=exc)

    with ThreadPoolExecutor(max_workers=len(connections)) as pool:
        return list(pool.map(worker, range(len(connections))))


def wait_until_blocked(observer: psycopg.Connection, backend_pid: int, timeout: float = 5.0) -> bool:
    """Return True once PostgreSQL reports ``backend_pid`` is waiting on a lock."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = observer.execute(
            "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (backend_pid,)
        ).fetchone()
        if row and row["wait_event_type"] == "Lock":
            return True
        time.sleep(0.01)
    return False


class Background:
    """Run a callable on another thread and collect its outcome."""

    def __init__(self, fn: Callable[[], Any]) -> None:
        self.outcome: Outcome | None = None
        self._thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self._thread.start()

    def _run(self, fn: Callable[[], Any]) -> None:
        try:
            self.outcome = Outcome(0, value=fn())
        except Exception as exc:
            self.outcome = Outcome(0, error=exc)

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def result(self, timeout: float = 10.0) -> Outcome:
        self._thread.join(timeout)
        assert not self._thread.is_alive(), "background transaction did not finish"
        assert self.outcome is not None
        return self.outcome
