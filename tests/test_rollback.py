"""Guarantee 4 — Rollback: a failure mid-operation leaves no partial state (I7)."""

from __future__ import annotations

import psycopg
import pytest

from app.services import audit, inventory, reservations
from tests.conftest import count, stock


class SimulatedCrash(RuntimeError):
    pass


def crash_after_inventory_update(monkeypatch, product_id, observed: list):
    """Replace the audit write (the step right after the inventory UPDATE) with a crash.

    Before crashing, record what the inventory looked like *inside* the
    transaction, proving the stock change really happened before the failure.
    """

    def failing_record(conn, **kwargs):
        row = conn.execute(
            "SELECT on_hand_quantity, reserved_quantity FROM inventory WHERE product_id = %s", (product_id,)
        ).fetchone()
        observed.append((row["on_hand_quantity"], row["reserved_quantity"]))
        raise SimulatedCrash("process died after updating inventory")

    monkeypatch.setattr(audit, "record", failing_record)


def test_failure_after_inventory_update_leaves_no_trace(world, conn, monkeypatch):
    observed: list = []
    crash_after_inventory_update(monkeypatch, world.cheese, observed)

    with pytest.raises(SimulatedCrash):
        reservations.create_reservation_idempotent(
            conn, world.eli, "key-1", product_id=world.cheese, quantity=2, order_reference="order"
        )

    assert observed == [(5, 2)], "the inventory UPDATE should have run before the crash"
    assert stock(conn, world.cheese) == (5, 0)
    assert count(conn, "SELECT 1 FROM reservations") == 0
    assert count(conn, "SELECT 1 FROM audit_events WHERE action = 'reserve'") == 0
    assert count(conn, "SELECT 1 FROM idempotency_records") == 0
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_failure_during_fulfillment_leaves_reservation_active(world, conn, monkeypatch):
    rid = reservations.create_reservation(
        conn, world.eli, product_id=world.cheese, quantity=2, order_reference="order"
    )["reservation"]["id"]

    observed: list = []
    crash_after_inventory_update(monkeypatch, world.cheese, observed)
    with pytest.raises(SimulatedCrash):
        reservations.fulfill(conn, world.eli, rid)

    assert observed == [(3, 0)]
    assert stock(conn, world.cheese) == (5, 2)
    status = conn.execute("SELECT status FROM reservations WHERE id = %s", (rid,)).fetchone()["status"]
    assert status == "active"


def test_failure_during_delivery_leaves_stock_unchanged(world, conn, monkeypatch):
    observed: list = []
    crash_after_inventory_update(monkeypatch, world.cheese, observed)
    with pytest.raises(SimulatedCrash):
        inventory.receive(conn, world.manager, world.cheese, 10, "Delivery")
    assert observed == [(15, 0)]
    assert stock(conn, world.cheese) == (5, 0)


def test_rejected_spoilage_leaves_stock_unchanged(world, conn):
    from app.errors import InsufficientStock

    reservations.create_reservation(conn, world.eli, product_id=world.cheese, quantity=4, order_reference="order")
    with pytest.raises(InsufficientStock):
        inventory.adjust(conn, world.manager, world.cheese, -2, "Mould found")  # would leave 3 < 4 reserved
    assert stock(conn, world.cheese) == (5, 4)

    inventory.adjust(conn, world.manager, world.cheese, -1, "Mould found")  # 4 >= 4 reserved: allowed
    assert stock(conn, world.cheese) == (4, 4)


@pytest.mark.parametrize(
    ("statement", "constraint"),
    [
        ("UPDATE inventory SET on_hand_quantity = -1, reserved_quantity = 0", "inventory_on_hand_non_negative"),
        ("UPDATE inventory SET reserved_quantity = -1", "inventory_reserved_non_negative"),
        ("UPDATE inventory SET reserved_quantity = on_hand_quantity + 1", "inventory_reserved_within_on_hand"),
    ],
)
def test_check_constraints_are_a_second_line_of_defence(world, conn, statement, constraint):
    with pytest.raises(psycopg.errors.CheckViolation) as exc_info:
        conn.execute(statement)
    assert exc_info.value.diag.constraint_name == constraint
    assert stock(conn, world.cheese) == (5, 0)


def test_audit_log_is_append_only(world, conn):
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("UPDATE audit_events SET reason = 'tampered'")
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("DELETE FROM audit_events")
