"""Guarantee 3 — Competing transitions: a reservation closes exactly once (I4)."""

from __future__ import annotations

import pytest

from app.errors import InvalidTransition
from app.services import reservations
from tests.concurrency import Background, run_concurrently, wait_until_blocked
from tests.conftest import count, stock


def new_reservation(conn, actor, product_id, quantity=2, ttl_minutes=30):
    return reservations.create_reservation(
        conn, actor, product_id=product_id, quantity=quantity, order_reference="order", ttl_minutes=ttl_minutes
    )["reservation"]["id"]


def force_past_due(conn, reservation_id):
    conn.execute("UPDATE reservations SET expires_at = now() - interval '1 second' WHERE id = %s", (reservation_id,))


def test_cancel_and_fulfill_race_only_one_wins(world, conn, new_conn):
    for i in range(20):
        on_hand_before, _ = stock(conn, world.cheese)
        if on_hand_before < 2:
            from app.services import inventory

            inventory.receive(conn, world.manager, world.cheese, 5, "Restock between rounds")
            on_hand_before, _ = stock(conn, world.cheese)

        rid = new_reservation(conn, world.eli, world.cheese)
        ops = [
            lambda c: reservations.cancel(c, world.eli, rid),  # employee cancels their order
            lambda c: reservations.fulfill(c, world.manager, rid),  # manager ships it
        ]

        outcomes = run_concurrently([new_conn(), new_conn()], lambda c, n: ops[n](c))

        winners = [o for o in outcomes if o.ok]
        losers = [o for o in outcomes if not o.ok]
        assert len(winners) == 1, f"round {i}: {outcomes}"
        assert isinstance(losers[0].error, InvalidTransition)

        status = winners[0].value["reservation"]["status"]
        expected_on_hand = on_hand_before if status == "cancelled" else on_hand_before - 2
        assert stock(conn, world.cheese) == (expected_on_hand, 0)
        assert count(
            conn,
            "SELECT 1 FROM audit_events WHERE reservation_id = %s AND action IN ('cancel','fulfill')",
            (rid,),
        ) == 1


def test_second_transition_waits_on_the_row_lock_and_sees_the_new_status(world, conn, new_conn):
    rid = new_reservation(conn, world.eli, world.cheese)
    conn_a, conn_b = new_conn(), new_conn()

    with conn_a.transaction():
        reservations.cancel(conn_a, world.eli, rid)
        b = Background(lambda: reservations.fulfill(conn_b, world.manager, rid))
        assert wait_until_blocked(conn, conn_b.info.backend_pid), "fulfill never waited on the reservation lock"

    outcome = b.result()
    assert isinstance(outcome.error, InvalidTransition)
    assert outcome.error.details["current_status"] == "cancelled"
    assert stock(conn, world.cheese) == (5, 0)


@pytest.mark.parametrize("first", ["cancel", "fulfill", "expire"])
@pytest.mark.parametrize("second", ["cancel", "fulfill", "expire"])
def test_terminal_states_are_final(world, conn, first, second):
    rid = new_reservation(conn, world.eli, world.cheese)

    def do(action):
        if action == "cancel":
            return reservations.cancel(conn, world.eli, rid)
        if action == "fulfill":
            return reservations.fulfill(conn, world.eli, rid)
        force_past_due(conn, rid)
        assert reservations.expire_due(conn) == 1
        return None

    do(first)
    after_first = stock(conn, world.cheese)

    if second == "expire":
        force_past_due(conn, rid)
        assert reservations.expire_due(conn) == 0
    else:
        with pytest.raises(InvalidTransition):
            do(second)
    assert stock(conn, world.cheese) == after_first


def test_operation_table(world, conn):
    """Reserve +2 reserved; cancel -2 reserved; fulfill -2 physical & -2 reserved."""
    r1 = new_reservation(conn, world.eli, world.cheese)
    assert stock(conn, world.cheese) == (5, 2)
    reservations.cancel(conn, world.eli, r1)
    assert stock(conn, world.cheese) == (5, 0)
    r2 = new_reservation(conn, world.eli, world.cheese)
    reservations.fulfill(conn, world.eli, r2)
    assert stock(conn, world.cheese) == (3, 0)


def test_expiry_releases_reserved_stock_and_is_audited_as_system(world, conn):
    rid = new_reservation(conn, world.eli, world.cheese, quantity=3)
    new_reservation(conn, world.erin, world.cheese, quantity=1)  # not due
    force_past_due(conn, rid)

    assert reservations.expire_due(conn) == 1
    assert stock(conn, world.cheese) == (5, 1)
    event = conn.execute(
        "SELECT actor_id, reserved_delta FROM audit_events WHERE reservation_id = %s AND action = 'expire'", (rid,)
    ).fetchone()
    assert event == {"actor_id": None, "reserved_delta": -3}


def test_fulfilling_a_past_due_reservation_is_rejected(world, conn):
    rid = new_reservation(conn, world.eli, world.cheese)
    force_past_due(conn, rid)
    with pytest.raises(InvalidTransition):
        reservations.fulfill(conn, world.eli, rid)
    assert stock(conn, world.cheese) == (5, 2)


def test_expiry_racing_with_cancel_closes_once(world, conn, new_conn):
    for _ in range(10):
        rid = new_reservation(conn, world.eli, world.cheese, quantity=1)
        force_past_due(conn, rid)
        ops = [lambda c: reservations.expire_due(c), lambda c: reservations.cancel(c, world.eli, rid)]
        run_concurrently([new_conn(), new_conn()], lambda c, n: ops[n](c))
        assert count(
            conn,
            "SELECT 1 FROM audit_events WHERE reservation_id = %s AND action IN ('cancel','expire')",
            (rid,),
        ) == 1
    assert stock(conn, world.cheese) == (5, 0)
