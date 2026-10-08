"""Guarantee 9 & 10 — Observability, Metrics, and PostgreSQL-backed Rate Limiting.

Proves:
1. Health and readiness probes (/healthz, /readyz) and Prometheus/system metrics (/metrics).
2. X-Request-ID propagation via HTTP middleware.
3. PostgreSQL-backed login rate limiting: 5 consecutive invalid credentials trigger HTTP 429.
4. Production secrets hygiene: attempting to run in production with default secrets is blocked.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings


def test_healthz_and_readyz(database_url):
    app = create_app(Settings(database_url=database_url, expiry_sweep_seconds=0))
    with TestClient(app) as client:
        # 1. Healthz
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert "X-Request-ID" in resp.headers

        # 2. Readyz
        ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"

        # 3. Custom request ID passed through
        custom_resp = client.get("/healthz", headers={"X-Request-ID": "test-req-123"})
        assert custom_resp.headers["X-Request-ID"] == "test-req-123"


def test_metrics_endpoint(database_url, world):
    app = create_app(Settings(database_url=database_url, expiry_sweep_seconds=0))
    with TestClient(app) as client:
        resp = client.get("/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "inventory" in data
        assert "reservations" in data
        assert "audit_events" in data
        assert "idempotency_records" in data
        assert data["inventory"]["total_products"] >= 1


def test_login_rate_limiting(database_url, world):
    app = create_app(Settings(database_url=database_url, expiry_sweep_seconds=0))
    with TestClient(app) as client:
        headers = {"X-Forwarded-For": "198.51.100.42"}

        # 5 failed login attempts
        for _ in range(5):
            resp = client.post("/auth/token", data={"username": "eli", "password": "wrong-password"}, headers=headers)
            assert resp.status_code == 401

        # 6th attempt is blocked by PostgreSQL rate limiter (HTTP 429)
        blocked = client.post("/auth/token", data={"username": "eli", "password": "wrong-password"}, headers=headers)
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "too_many_requests"

        # A different IP address is not blocked
        other_ip = client.post(
            "/auth/token",
            data={"username": "maria", "password": "manager-pass"},
            headers={"X-Forwarded-For": "198.51.100.99"},
        )
        assert other_ip.status_code == 200


def test_production_secret_enforcement(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("JWT_SECRET", raising=False)

    with pytest.raises(ValueError, match="JWT_SECRET must be explicitly set"):
        Settings.from_env()
