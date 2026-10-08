"""Guarantee 5 — Validation and permissions, end to end through HTTP."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from tests.conftest import stock


@pytest.fixture(scope="session")
def client(database_url):
    app = create_app(Settings(database_url=database_url, expiry_sweep_seconds=0, pool_max_size=10))
    with TestClient(app) as c:
        yield c


@pytest.fixture
def api(client, world):
    tokens: dict[str, str] = {}

    def login(username: str, password: str):
        return client.post("/auth/token", data={"username": username, "password": password})

    def headers(username: str, key: str | None = None) -> dict[str, str]:
        if username not in tokens:
            password = "manager-pass" if username == "maria" else "employee-pass"
            tokens[username] = login(username, password).json()["access_token"]
        h = {"Authorization": f"Bearer {tokens[username]}"}
        if key:
            h["Idempotency-Key"] = key
        return h

    def reserve(username: str, quantity=2, key=None, **extra):
        body = {"product_id": world.cheese, "quantity": quantity, "order_reference": "order-7", **extra}
        return client.post("/reservations", json=body, headers=headers(username, key or str(uuid.uuid4())))

    class Api:
        pass

    a = Api()
    a.client, a.login, a.headers, a.reserve, a.world = client, login, headers, reserve, world
    return a


def error_code(response) -> str:
    return response.json()["error"]["code"]


# ------------------------------------------------------------------ authentication
def test_login_rejects_wrong_password(api):
    r = api.login("eli", "wrong")
    assert r.status_code == 401 and error_code(r) == "unauthenticated"


def test_requests_without_a_token_are_rejected(api):
    assert api.client.get("/products").status_code == 401
    r = api.client.get("/products", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


# ------------------------------------------------------------------ validation
@pytest.mark.parametrize("quantity", [0, -1, 1.5, "2", True, None])
def test_invalid_quantities_are_rejected(api, conn, quantity):
    r = api.reserve("eli", quantity=quantity)
    assert r.status_code == 422
    assert stock(conn, api.world.cheese) == (5, 0)


def test_idempotency_key_is_required(api, conn):
    body = {"product_id": api.world.cheese, "quantity": 1, "order_reference": "x"}
    r = api.client.post("/reservations", json=body, headers=api.headers("eli"))
    assert r.status_code == 422 and error_code(r) == "invalid_request"
    assert stock(conn, api.world.cheese) == (5, 0)


def test_insufficient_stock_and_unknown_product(api):
    r = api.reserve("eli", quantity=6)
    assert r.status_code == 409 and error_code(r) == "insufficient_stock"
    assert r.json()["error"]["details"]["available_quantity"] == 5

    r = api.reserve("eli", product_id=424242)
    assert r.status_code == 404


def test_inventory_reports_available_as_on_hand_minus_reserved(api):
    api.reserve("eli", quantity=2)
    inv = api.client.get(f"/products/{api.world.cheese}/inventory", headers=api.headers("eli")).json()
    assert (inv["on_hand_quantity"], inv["reserved_quantity"], inv["available_quantity"]) == (5, 2, 3)


# ------------------------------------------------------------------ idempotency over HTTP
def test_http_retry_replays_original_response(api, conn):
    first = api.reserve("eli", key="retry-me")
    second = api.reserve("eli", key="retry-me")
    assert first.status_code == second.status_code == 201
    assert first.headers["Idempotent-Replayed"] == "false"
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.json() == first.json()
    assert stock(conn, api.world.cheese) == (5, 2)

    changed = api.reserve("eli", key="retry-me", quantity=1)
    assert changed.status_code == 409 and error_code(changed) == "idempotency_key_reused"


# ------------------------------------------------------------------ role permissions
def test_employees_cannot_change_physical_stock(api, conn):
    cheese = api.world.cheese
    r = api.client.post(f"/inventory/{cheese}/receive", json={"quantity": 5}, headers=api.headers("eli"))
    assert r.status_code == 403
    r = api.client.post(
        f"/inventory/{cheese}/adjust", json={"delta": -1, "reason": "spoiled"}, headers=api.headers("eli")
    )
    assert r.status_code == 403
    assert stock(conn, cheese) == (5, 0)


def test_employees_cannot_read_audit_or_force_expiry(api):
    assert api.client.get("/audit-events", headers=api.headers("eli")).status_code == 403
    assert api.client.post("/reservations/expire", headers=api.headers("eli")).status_code == 403


def test_manager_receives_delivery_and_records_spoilage(api, conn):
    cheese = api.world.cheese
    r = api.client.post(
        f"/inventory/{cheese}/receive", json={"quantity": 3, "reason": "Tuesday delivery"}, headers=api.headers("maria")
    )
    assert r.status_code == 200 and r.json()["on_hand_quantity"] == 8

    r = api.client.post(
        f"/inventory/{cheese}/adjust", json={"delta": -1, "reason": "Mould"}, headers=api.headers("maria")
    )
    assert r.status_code == 200 and r.json()["on_hand_quantity"] == 7

    events = api.client.get("/audit-events", headers=api.headers("maria")).json()
    assert [e["action"] for e in events[:2]] == ["adjust", "receive"]
    assert events[0]["reason"] == "Mould" and events[0]["actor_username"] == "maria"


@pytest.mark.parametrize("body", [{"delta": 0, "reason": "x"}, {"delta": -1, "reason": ""}, {"delta": -1}])
def test_invalid_adjustments_are_rejected(api, conn, body):
    r = api.client.post(f"/inventory/{api.world.cheese}/adjust", json=body, headers=api.headers("maria"))
    assert r.status_code == 422
    assert stock(conn, api.world.cheese) == (5, 0)


def test_spoilage_cannot_undercut_reservations(api, conn):
    api.reserve("eli", quantity=4)
    r = api.client.post(
        f"/inventory/{api.world.cheese}/adjust", json={"delta": -2, "reason": "Mould"}, headers=api.headers("maria")
    )
    assert r.status_code == 409 and error_code(r) == "insufficient_stock"
    assert stock(conn, api.world.cheese) == (5, 4)


# ------------------------------------------------------------------ ownership
def test_employee_cannot_see_or_touch_another_employees_reservation(api, conn):
    rid = api.reserve("eli").json()["reservation"]["id"]

    assert api.client.get(f"/reservations/{rid}", headers=api.headers("erin")).status_code == 404
    assert api.client.post(f"/reservations/{rid}/cancel", headers=api.headers("erin")).status_code == 404
    assert api.client.post(f"/reservations/{rid}/fulfill", headers=api.headers("erin")).status_code == 404
    assert stock(conn, api.world.cheese) == (5, 2)

    listing = api.client.get("/reservations", headers=api.headers("erin")).json()
    assert listing == []


def test_owner_and_manager_can_manage_a_reservation(api, conn):
    r1 = api.reserve("eli").json()["reservation"]["id"]
    r2 = api.reserve("eli").json()["reservation"]["id"]

    assert api.client.get(f"/reservations/{r1}", headers=api.headers("eli")).status_code == 200
    assert api.client.post(f"/reservations/{r1}/cancel", headers=api.headers("eli")).status_code == 200
    r = api.client.post(f"/reservations/{r2}/fulfill", headers=api.headers("maria"))
    assert r.status_code == 200 and r.json()["reservation"]["status"] == "fulfilled"
    assert stock(conn, api.world.cheese) == (3, 0)

    again = api.client.post(f"/reservations/{r1}/fulfill", headers=api.headers("eli"))
    assert again.status_code == 409 and error_code(again) == "invalid_transition"

    all_for_manager = api.client.get("/reservations", headers=api.headers("maria")).json()
    assert {r["id"] for r in all_for_manager} == {r1, r2}
