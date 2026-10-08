"""Guarantee 1 — Last item: concurrent reservations can never oversell (I2)."""

from __future__ import annotations

import psycopg
import pytest

from app.errors import InsufficientStock
from app.services import reservations
from tests.concurrency import Background, run_concurrently, wait_until_blocked
from tests.conftest import count, make_product, stock


def test_two_connections_race_for_the_last_unit(world, conn, new_conn):
    """Two independent connections reserve the single remaining unit at the same instant.

    Repeated over many rounds so the barrier produces genuine contention.
    """
    rounds = 25
    for i in range(rounds):
        product = make_product(conn, world.manager, f"LAST-{i}", on_hand=1)
        clients = [new_conn(), new_conn()]
        actors = [world.eli, world.erin]

        outcomes = run_concurrently(
            clients,
            lambda c, n: reservations.create_reservation(
                c, actors[n], product_id=product, quantity=1, order_reference=f"order-{i}-{n}"
            ),
        )

        winners = [o for o in outcomes if o.ok]
        losers = [o for o in outcomes if not o.ok]
        assert len(winners) == 1, f"round {i}: expected exactly one winner, got {outcomes}"
        assert len(losers) == 1 and isinstance(losers[0].error, InsufficientStock)
        assert stock(conn, product) == (1, 1)
        assert count(conn, "SELECT 1 FROM reservations WHERE product_id = %s", (product,)) == 1


def test_second_transaction_waits_for_the_first_then_rechecks(world, conn, new_conn):
    """Deterministic version: prove the transactions overlapped and how PostgreSQL resolved it.

    A takes the last unit and holds its transaction open. B starts and PostgreSQL
    reports B is *blocked on A's row lock*. When A commits, B's UPDATE re-evaluates
    ``on_hand - reserved >= 1`` against the committed row, matches nothing, and B fails.
    """
    product = make_product(conn, world.manager, "LAST-DETERMINISTIC", on_hand=1)
    conn_a, conn_b = new_conn(), new_conn()

    with conn_a.transaction():
        reservations.create_reservation(conn_a, world.eli, product_id=product, quantity=1, order_reference="A")

        b = Background(
            lambda: reservations.create_reservation(
                conn_b, world.erin, product_id=product, quantity=1, order_reference="B"
            )
        )
        assert wait_until_blocked(conn, conn_b.info.backend_pid), "B never blocked on A's lock"
        assert b.is_alive()
        # A commits when this block exits.

    outcome = b.result()
    assert isinstance(outcome.error, InsufficientStock)
    assert outcome.error.details["available_quantity"] == 0
    assert stock(conn, product) == (1, 1)


def test_many_clients_competing_for_five_cases_never_oversell(world, conn, new_conn):
    """20 employees each try to grab 1 case of the 5 in stock at once: exactly 5 succeed."""
    clients = [new_conn() for _ in range(20)]
    actors = [world.eli, world.erin]

    outcomes = run_concurrently(
        clients,
        lambda c, n: reservations.create_reservation(
            c, actors[n % 2], product_id=world.cheese, quantity=1, order_reference=f"order-{n}"
        ),
    )

    assert sum(o.ok for o in outcomes) == 5
    assert all(isinstance(o.error, InsufficientStock) for o in outcomes if not o.ok)
    assert stock(conn, world.cheese) == (5, 5)


def test_mixed_quantities_never_exceed_physical_stock(world, conn, new_conn):
    clients = [new_conn() for _ in range(12)]
    quantities = [1, 2, 3] * 4

    outcomes = run_concurrently(
        clients,
        lambda c, n: reservations.create_reservation(
            c, world.eli, product_id=world.cheese, quantity=quantities[n], order_reference=f"mixed-{n}"
        ),
    )

    reserved = sum(quantities[o.worker] for o in outcomes if o.ok)
    assert 0 < reserved <= 5
    assert stock(conn, world.cheese) == (5, reserved)


def test_naive_read_then_write_is_stale_and_the_check_constraint_catches_it(world, conn, new_conn):
    """Why the check must live in the UPDATE.

    The naive pattern — SELECT available, check in Python, then UPDATE — lets both
    clients see the last unit. The second write would oversell; the CHECK
    constraint (second line of defence) is what stops it.
    """
    product = make_product(conn, world.manager, "NAIVE", on_hand=1)
    conn_a, conn_b = new_conn(), new_conn()

    def naive_read(c):
        return c.execute("SELECT available_quantity FROM inventory WHERE product_id = %s", (product,)).fetchone()[
            "available_quantity"
        ]

    # Both clients read before either writes: both believe one unit is free.
    assert naive_read(conn_a) == 1
    assert naive_read(conn_b) == 1

    naive_write = "UPDATE inventory SET reserved_quantity = reserved_quantity + 1 WHERE product_id = %s"
    conn_a.execute(naive_write, (product,))  # A "reserves" (raw write, no audit: rolled back below)

    with pytest.raises(psycopg.errors.CheckViolation) as exc_info:
        conn_b.execute(naive_write, (product,))
    assert exc_info.value.diag.constraint_name == "inventory_reserved_within_on_hand"

    # Undo A's raw, un-audited write so the invariant sweep stays meaningful.
    conn_a.execute("UPDATE inventory SET reserved_quantity = reserved_quantity - 1 WHERE product_id = %s", (product,))
    assert stock(conn, product) == (1, 0)


def test_unknown_product_is_distinguished_from_insufficient_stock(world, conn):
    from app.errors import NotFound

    with pytest.raises(NotFound):
        reservations.create_reservation(conn, world.eli, product_id=999_999, quantity=1, order_reference="x")
    with pytest.raises(InsufficientStock):
        reservations.create_reservation(conn, world.eli, product_id=world.cheese, quantity=6, order_reference="x")
    assert stock(conn, world.cheese) == (5, 0)
