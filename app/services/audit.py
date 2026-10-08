"""Audit events. Always called inside the transaction that changed stock (I6)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.db import DBConn
from app.services.common import audit_out


def record(
    conn: DBConn,
    *,
    actor_id: int | None,
    action: str,
    product_id: int,
    on_hand_delta: int,
    reserved_delta: int,
    inventory_after: Mapping[str, Any],
    reservation_id: int | None = None,
    reason: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_events (actor_id, action, product_id, reservation_id,
                                  on_hand_delta, reserved_delta,
                                  on_hand_after, reserved_after, reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            actor_id,
            action,
            product_id,
            reservation_id,
            on_hand_delta,
            reserved_delta,
            inventory_after["on_hand_quantity"],
            inventory_after["reserved_quantity"],
            reason,
        ),
    )


def list_events(conn: DBConn, *, product_id: int | None = None, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        """
        SELECT a.*, u.username AS actor_username, p.sku
        FROM audit_events a
        JOIN products p ON p.id = a.product_id
        LEFT JOIN users u ON u.id = a.actor_id
        WHERE (%(product_id)s::bigint IS NULL OR a.product_id = %(product_id)s)
        ORDER BY a.id DESC
        LIMIT %(limit)s
        """,
        {"product_id": product_id, "limit": limit},
    ).fetchall()
    return [audit_out(r) for r in rows]
