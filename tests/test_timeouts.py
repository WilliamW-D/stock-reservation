"""Tests for operational safety: lock timeouts, retry-after headers, and retry helper."""

from __future__ import annotations

import time

import psycopg
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.services.retry import execute_with_retry
from tests.conftest import stock


def test_lock_timeout_maps_to_503_and_leaves_stock_unchanged(world, conn, new_conn, database_url):
    """When a transaction holds a row lock beyond lock_timeout, competing requests fail fast with 503."""
    app = create_app(
        Settings(
            database_url=database_url,
            expiry_sweep_seconds=0,
            pool_max_size=5,
            lock_timeout_ms=400,  # fast timeout for test
            statement_timeout_ms=2000,
        )
    )

    holder = new_conn()
    with holder.transaction():
        # Lock the cheese inventory row
        holder.execute("SELECT * FROM inventory WHERE product_id = %s FOR UPDATE", (world.cheese,))

        with TestClient(app) as client:
            token = client.post("/auth/token", data={"username": "eli", "password": "employee-pass"}).json()[
                "access_token"
            ]

            t0 = time.monotonic()
            response = client.post(
                "/reservations",
                json={"product_id": world.cheese, "quantity": 1, "order_reference": "timeout-test"},
                headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "timeout-key"},
            )
            elapsed = time.monotonic() - t0

            # It must fail within reasonable time (e.g. ~400ms + margin), not hang indefinitely
            assert 0.35 <= elapsed <= 2.5
            assert response.status_code == 503
            data = response.json()
            assert data["error"]["code"] == "database_timeout"
            assert response.headers.get("retry-after") == "1"

    # After holder finishes, stock remains completely unchanged
    assert stock(conn, world.cheese) == (5, 0)


def test_execute_with_retry_succeeds_after_transient_conflict():
    attempts = 0

    class DummyError(psycopg.Error):
        def __init__(self, code: str):
            super().__init__()
            self.sqlstate = code

    def flaky_work():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise DummyError("40P01")  # deadlock detected
        return "success"

    result = execute_with_retry(flaky_work, max_retries=3, base_backoff_sec=0.01)
    assert result == "success"
    assert attempts == 3
