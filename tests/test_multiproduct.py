"""Guarantee 5 — Multi-product orders & deadlock prevention.

Proves:
1. Atomic multi-item reservations: all-or-nothing stock allocation across multiple products.
2. Deadlock prevention: concurrent requests with opposing item orderings ({A, B} vs {B, A})
   never trigger PostgreSQL deadlocks because row locks are acquired in sorted product_id order.
3. Line-level fulfillment: support for partial and complete fulfillment of individual order lines.
4. Cancellation of partially-fulfilled orders: only unfulfilled stock is restored.
5. Invariant assertion holds after every transition.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.errors import InsufficientStock, InvalidRequest
from app.services import reservations
from tests.concurrency import run_concurrently
from tests.conftest import make_product, stock


def test_multiproduct_atomic_success(world, conn):
    butter = make_product(conn, world.manager, "BUTTER", on_hand=10)
    items = [
        {"product_id": world.cheese, "quantity": 2},
        {"product_id": butter, "quantity": 4},
    ]

    res = reservations.create_reservation(conn, world.eli, items=items, order_reference="COMBO-1")
    assert res["reservation"]["status"] == "active"
    assert len(res["reservation"]["items"]) == 2

    assert stock(conn, world.cheese) == (5, 2)
    assert stock(conn, butter) == (10, 4)

    # Check line statuses
    lines = res["reservation"]["items"]
    assert all(line["status"] == "active" for line in lines)
    assert {item["product_id"]: item["quantity"] for item in lines} == {world.cheese: 2, butter: 4}


def test_multiproduct_all_or_nothing_rollback(world, conn):
    butter = make_product(conn, world.manager, "BUTTER-LOW", on_hand=2)
    items = [
        {"product_id": world.cheese, "quantity": 2},  # cheese has 5, would succeed
        {"product_id": butter, "quantity": 5},  # butter only has 2, will fail
    ]

    with pytest.raises(InsufficientStock) as exc_info:
        reservations.create_reservation(conn, world.eli, items=items, order_reference="COMBO-FAIL")

    err = exc_info.value
    assert err.details.get("shortfalls") is not None

    # Guarantee: cheese stock was NOT modified even though it had sufficient available quantity
    assert stock(conn, world.cheese) == (5, 0)
    assert stock(conn, butter) == (2, 0)


def test_concurrent_opposing_orders_deadlock_free(world, conn, new_conn):
    """Stress test deadlock prevention:

    Two workers simultaneously request the same two products in reverse order:
      Worker 1: [pA, pB]
      Worker 2: [pB, pA]
    Without sorted lock acquisition, this is a textbook PostgreSQL deadlock (40P01).
    With sorted lock acquisition, one worker always locks both and the other cleanly waits.
    """
    p_a = make_product(conn, world.manager, "PRODUCT-A", on_hand=50)
    p_b = make_product(conn, world.manager, "PRODUCT-B", on_hand=50)

    rounds = 15
    for i in range(rounds):
        clients = [new_conn(), new_conn()]
        items_1 = [{"product_id": p_a, "quantity": 1}, {"product_id": p_b, "quantity": 1}]
        items_2 = [{"product_id": p_b, "quantity": 1}, {"product_id": p_a, "quantity": 1}]

        def do_reserve(c, n, r=i, i1=items_1, i2=items_2):
            req_items = i1 if n == 0 else i2
            user = world.eli if n == 0 else world.erin
            return reservations.create_reservation(c, user, items=req_items, order_reference=f"race-{r}-{n}")

        outcomes = run_concurrently(clients, do_reserve)
        assert all(o.ok for o in outcomes), f"Deadlock or error encountered: {outcomes}"


def test_line_level_partial_fulfillment(world, conn):
    butter = make_product(conn, world.manager, "BUTTER-FULFILL", on_hand=10)
    items = [
        {"product_id": world.cheese, "quantity": 4},
        {"product_id": butter, "quantity": 2},
    ]

    res = reservations.create_reservation(conn, world.eli, items=items, order_reference="PARTIAL-ORDER")
    res_id = res["reservation"]["id"]
    lines = res["reservation"]["items"]
    cheese_line = next(it for it in lines if it["product_id"] == world.cheese)
    butter_line = next(it for it in lines if it["product_id"] == butter)

    # 1. Partially fulfill 2 of 4 units of cheese line
    res_part = reservations.fulfill_line(conn, world.eli, res_id, cheese_line["id"], quantity=2)
    assert res_part["reservation"]["status"] == "partially_fulfilled"
    c_line_updated = next(it for it in res_part["reservation"]["items"] if it["id"] == cheese_line["id"])
    assert c_line_updated["fulfilled_quantity"] == 2
    assert c_line_updated["status"] == "partially_fulfilled"
    # Cheese physical stock reduced by 2, reserved reduced by 2
    assert stock(conn, world.cheese) == (3, 2)

    # 2. Over-fulfilling the remaining line quantity should be rejected
    with pytest.raises(InvalidRequest):
        reservations.fulfill_line(conn, world.eli, res_id, cheese_line["id"], quantity=5)

    # 3. Fulfill the remaining 2 units of cheese line
    res_part2 = reservations.fulfill_line(conn, world.eli, res_id, cheese_line["id"], quantity=2)
    c_line_done = next(it for it in res_part2["reservation"]["items"] if it["id"] == cheese_line["id"])
    assert c_line_done["fulfilled_quantity"] == 4
    assert c_line_done["status"] == "fulfilled"
    # Reservation is still partially_fulfilled because butter line is still active
    assert res_part2["reservation"]["status"] == "partially_fulfilled"
    assert stock(conn, world.cheese) == (1, 0)

    # 4. Fulfill butter line completely (without quantity arg, defaults to remaining)
    res_final = reservations.fulfill_line(conn, world.eli, res_id, butter_line["id"])
    assert res_final["reservation"]["status"] == "fulfilled"
    assert res_final["reservation"]["closed_at"] is not None
    assert stock(conn, butter) == (8, 0)


def test_cancel_partially_fulfilled_order(world, conn):
    butter = make_product(conn, world.manager, "BUTTER-CANCEL", on_hand=10)
    items = [
        {"product_id": world.cheese, "quantity": 4},
        {"product_id": butter, "quantity": 2},
    ]

    res = reservations.create_reservation(conn, world.eli, items=items, order_reference="CANCEL-ORDER")
    res_id = res["reservation"]["id"]
    lines = res["reservation"]["items"]
    cheese_line = next(it for it in lines if it["product_id"] == world.cheese)

    # Fulfill 1 unit of cheese
    reservations.fulfill_line(conn, world.eli, res_id, cheese_line["id"], quantity=1)
    assert stock(conn, world.cheese) == (4, 3)
    assert stock(conn, butter) == (10, 2)

    # Cancel the reservation
    cancelled = reservations.cancel(conn, world.eli, res_id, reason="Customer cancelled remaining items")
    assert cancelled["reservation"]["status"] == "cancelled"

    # Only remaining reserved quantities were released:
    # Cheese: physical was 4, remaining 3 reserved released -> (4, 0)
    # Butter: physical was 10, remaining 2 reserved released -> (10, 0)
    assert stock(conn, world.cheese) == (4, 0)
    assert stock(conn, butter) == (10, 0)


def test_api_multiproduct_endpoints(world, conn, database_url):
    butter = make_product(conn, world.manager, "BUTTER-API", on_hand=8)
    app = create_app(Settings(database_url=database_url, expiry_sweep_seconds=0))

    with TestClient(app) as client:
        # Login as employee
        token = client.post("/auth/token", data={"username": "eli", "password": "employee-pass"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "multi-api-key-1"}

        # 1. Create multi-product reservation
        payload = {
            "order_reference": "API-ORDER-1",
            "items": [
                {"product_id": world.cheese, "quantity": 2},
                {"product_id": butter, "quantity": 3},
            ],
        }
        resp = client.post("/reservations", json=payload, headers=headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["reservation"]["status"] == "active"
        assert len(data["reservation"]["items"]) == 2
        res_id = data["reservation"]["id"]
        cheese_line = next(it for it in data["reservation"]["items"] if it["product_id"] == world.cheese)

        # 2. Replay with same idempotency key
        replay = client.post("/reservations", json=payload, headers=headers)
        assert replay.status_code == 201
        assert replay.headers.get("Idempotent-Replayed") == "true"
        assert replay.json()["reservation"]["id"] == res_id

        # 3. Fulfill line via HTTP
        line_resp = client.post(
            f"/reservations/{res_id}/lines/{cheese_line['id']}/fulfill",
            json={"quantity": 1, "reason": "Delivered 1 case early"},
            headers=headers,
        )
        assert line_resp.status_code == 200
        line_data = line_resp.json()
        assert line_data["reservation"]["status"] == "partially_fulfilled"
