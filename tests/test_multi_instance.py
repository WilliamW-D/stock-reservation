"""Guarantee 6 — Multi-instance horizontal scalability.

Proves:
1. Two completely distinct application instances (App A and App B) sharing the same PostgreSQL
   cluster enforce all correctness guarantees without shared in-memory state.
2. Idempotency across instances: a request created via Instance A is cleanly replayed when sent to Instance B.
3. Concurrency across instances: simultaneous requests across separate app processes racing for the last item
   never oversell.
4. Concurrent sweepers across instances: background sweepers in both instances use FOR UPDATE SKIP LOCKED
   to partition expired reservations with zero conflicts and zero double-processing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.services import reservations
from tests.concurrency import run_concurrently
from tests.conftest import count, make_product, stock


@pytest.fixture
def multi_apps(database_url):
    """Simulates two distinct application processes sharing the same database."""
    app_a = create_app(Settings(database_url=database_url, expiry_sweep_seconds=0))
    app_b = create_app(Settings(database_url=database_url, expiry_sweep_seconds=0))
    with TestClient(app_a) as client_a, TestClient(app_b) as client_b:
        yield client_a, client_b


def test_idempotency_shared_across_instances(world, conn, multi_apps):
    client_a, client_b = multi_apps

    token_a = client_a.post("/auth/token", data={"username": "eli", "password": "employee-pass"}).json()["access_token"]
    token_b = client_b.post("/auth/token", data={"username": "eli", "password": "employee-pass"}).json()["access_token"]

    key = "multi-instance-shared-key-1"
    payload = {
        "order_reference": "SHARED-ORDER",
        "product_id": world.cheese,
        "quantity": 2,
    }

    # 1. Send request to Instance A
    resp_a = client_a.post(
        "/reservations", json=payload, headers={"Authorization": f"Bearer {token_a}", "Idempotency-Key": key}
    )
    assert resp_a.status_code == 201
    assert resp_a.headers.get("Idempotent-Replayed") == "false"
    data_a = resp_a.json()
    res_id = data_a["reservation"]["id"]

    # 2. Send same key to Instance B
    resp_b = client_b.post(
        "/reservations", json=payload, headers={"Authorization": f"Bearer {token_b}", "Idempotency-Key": key}
    )
    assert resp_b.status_code == 201
    assert resp_b.headers.get("Idempotent-Replayed") == "true"
    data_b = resp_b.json()
    assert data_b["reservation"]["id"] == res_id

    # Physical stock was only reserved once
    assert stock(conn, world.cheese) == (5, 2)
    assert count(conn, "SELECT 1 FROM reservations WHERE order_reference = 'SHARED-ORDER'") == 1


def test_race_across_instances_for_last_unit(world, conn, multi_apps):
    client_a, client_b = multi_apps
    p = make_product(conn, world.manager, "LAST-INSTANCE-UNIT", on_hand=1)

    token_a = client_a.post("/auth/token", data={"username": "eli", "password": "employee-pass"}).json()["access_token"]
    token_b = client_b.post("/auth/token", data={"username": "erin", "password": "employee-pass"}).json()[
        "access_token"
    ]

    # Concurrently fire request 1 to Instance A and request 2 to Instance B
    def call_instance(c_idx):
        if c_idx == 0:
            return client_a.post(
                "/reservations",
                json={"order_reference": "RACE-A", "product_id": p, "quantity": 1},
                headers={"Authorization": f"Bearer {token_a}", "Idempotency-Key": "race-key-a"},
            )
        else:
            return client_b.post(
                "/reservations",
                json={"order_reference": "RACE-B", "product_id": p, "quantity": 1},
                headers={"Authorization": f"Bearer {token_b}", "Idempotency-Key": "race-key-b"},
            )

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = [ex.submit(call_instance, 0), ex.submit(call_instance, 1)]
        resps = [f.result() for f in futures]

    statuses = [r.status_code for r in resps]
    assert 201 in statuses, f"Expected one winner: {statuses}"
    assert 409 in statuses, f"Expected one loser (insufficient stock): {statuses}"
    assert stock(conn, p) == (1, 1)


def test_concurrent_sweepers_across_instances(world, conn, new_conn):
    """Simulates background sweepers running simultaneously on two distinct instances."""
    p = make_product(conn, world.manager, "MULTI-INSTANCE-SWEEP", on_hand=30)
    for i in range(10):
        res = reservations.create_reservation(conn, world.eli, product_id=p, quantity=1, order_reference=f"sweep-{i}")
        conn.execute(
            "UPDATE reservations SET expires_at = now() - interval '1 second' WHERE id = %s",
            (res["reservation"]["id"],),
        )

    # Sweeper in Instance A and Sweeper in Instance B run concurrently
    clients = [new_conn(), new_conn()]
    outcomes = run_concurrently(clients, lambda c, _: reservations.expire_due(c, limit=10))

    assert all(o.ok for o in outcomes)
    assert sum(o.value for o in outcomes) == 10
    assert stock(conn, p) == (30, 0)
