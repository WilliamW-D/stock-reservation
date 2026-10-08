"""Guarantee 2 — Duplicate requests: retries return the original result (I5)."""

from __future__ import annotations

import pytest

from app.errors import IdempotencyConflict, InsufficientStock, InvalidRequest
from app.services import idempotency, inventory, reservations
from tests.concurrency import Background, run_concurrently, wait_until_blocked
from tests.conftest import count, stock


def reserve(conn, actor, key, product_id, quantity=2, order_reference="order-42"):
    return reservations.create_reservation_idempotent(
        conn, actor, key, product_id=product_id, quantity=quantity, order_reference=order_reference
    )


def test_retry_returns_the_original_result(world, conn):
    first = reserve(conn, world.eli, "key-1", world.cheese)
    # Stock changes in between; the replay must still be the *original* response.
    inventory.receive(conn, world.manager, world.cheese, 3, "Delivery")
    second = reserve(conn, world.eli, "key-1", world.cheese)

    assert (first.status_code, first.replayed) == (201, False)
    assert (second.status_code, second.replayed) == (201, True)
    assert second.body == first.body
    assert first.body["inventory"]["available_quantity"] == 3
    assert stock(conn, world.cheese) == (8, 2)
    assert count(conn, "SELECT 1 FROM reservations") == 1


def test_same_key_with_different_contents_is_a_conflict(world, conn):
    reserve(conn, world.eli, "key-1", world.cheese, quantity=2)
    with pytest.raises(IdempotencyConflict):
        reserve(conn, world.eli, "key-1", world.cheese, quantity=3)
    assert stock(conn, world.cheese) == (5, 2)


def test_keys_are_scoped_per_user(world, conn):
    reserve(conn, world.eli, "shared-key", world.cheese)
    other = reserve(conn, world.erin, "shared-key", world.cheese)
    assert other.replayed is False
    assert count(conn, "SELECT 1 FROM reservations") == 2


def test_missing_key_is_rejected(world, conn):
    with pytest.raises(InvalidRequest):
        reserve(conn, world.eli, None, world.cheese)
    with pytest.raises(InvalidRequest):
        reserve(conn, world.eli, "   ", world.cheese)


def test_simultaneous_duplicates_create_one_reservation_and_one_stock_change(world, conn, new_conn):
    clients = [new_conn() for _ in range(10)]

    outcomes = run_concurrently(clients, lambda c, n: reserve(c, world.eli, "double-click", world.cheese))

    assert all(o.ok for o in outcomes), [o.error for o in outcomes if not o.ok]
    results = [o.value for o in outcomes]
    assert sum(not r.replayed for r in results) == 1
    assert all(r.body == results[0].body for r in results)
    assert count(conn, "SELECT 1 FROM reservations") == 1
    assert count(conn, "SELECT 1 FROM audit_events WHERE action = 'reserve'") == 1
    assert stock(conn, world.cheese) == (5, 2)


def test_duplicate_blocks_on_the_unique_key_until_the_first_commits(world, conn, new_conn):
    """Deterministic: B's key claim waits on A's uncommitted claim, then replays A's result."""
    conn_a, conn_b = new_conn(), new_conn()

    with conn_a.transaction():
        result_a = reserve(conn_a, world.eli, "slow-network", world.cheese)
        b = Background(lambda: reserve(conn_b, world.eli, "slow-network", world.cheese))
        assert wait_until_blocked(conn, conn_b.info.backend_pid), "duplicate never blocked on the key"

    outcome = b.result()
    assert outcome.ok
    assert outcome.value.replayed is True
    assert outcome.value.body == result_a.body
    assert stock(conn, world.cheese) == (5, 2)


def test_failed_attempt_does_not_burn_the_key(world, conn):
    """A rejected request rolls back its key claim, so a later retry can succeed."""
    with pytest.raises(InsufficientStock):
        reserve(conn, world.eli, "big-order", world.cheese, quantity=8)
    assert count(conn, "SELECT 1 FROM idempotency_records") == 0
    assert stock(conn, world.cheese) == (5, 0)

    inventory.receive(conn, world.manager, world.cheese, 5, "Delivery")
    retry = reserve(conn, world.eli, "big-order", world.cheese, quantity=8)
    assert retry.replayed is False and retry.status_code == 201
    assert stock(conn, world.cheese) == (10, 8)


def test_expired_idempotency_key_is_treated_as_fresh(world, conn):
    """After a key's retention window expires, reusing the key is accepted as a new operation."""
    key = "ttl-key-1"
    first = reserve(conn, world.eli, key, world.cheese, quantity=1)
    assert first.replayed is False
    first_res_id = first.body["reservation"]["id"]

    # Replay while unexpired -> replayed is True
    unexpired_replay = reserve(conn, world.eli, key, world.cheese, quantity=1)
    assert unexpired_replay.replayed is True
    assert unexpired_replay.body["reservation"]["id"] == first_res_id

    # Force expiration of the key
    conn.execute(
        "UPDATE idempotency_records SET expires_at = now() - interval '1 second' WHERE idempotency_key = %s", (key,)
    )

    # Replay after expiration -> treated as fresh request!
    fresh = reserve(conn, world.eli, key, world.cheese, quantity=2)
    assert fresh.replayed is False
    assert fresh.body["reservation"]["id"] != first_res_id
    assert stock(conn, world.cheese) == (5, 3)


def test_cleanup_expired_idempotency_records(world, conn):
    """Background cleanup purges expired records in batches using SKIP LOCKED."""
    reserve(conn, world.eli, "live-key", world.cheese, quantity=1)
    reserve(conn, world.eli, "dead-key-1", world.cheese, quantity=1)
    reserve(conn, world.eli, "dead-key-2", world.cheese, quantity=1)

    conn.execute(
        "UPDATE idempotency_records SET expires_at = now() - interval '1 second' WHERE idempotency_key LIKE 'dead-%'"
    )

    purged = idempotency.cleanup_expired(conn, limit=10)
    assert purged == 2
    assert count(conn, "SELECT 1 FROM idempotency_records WHERE idempotency_key LIKE %s", ("dead-%",)) == 0
    assert count(conn, "SELECT 1 FROM idempotency_records WHERE idempotency_key = 'live-key'") == 1
