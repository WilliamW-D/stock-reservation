"""Reservations: create (reserve), cancel, fulfill, expire.

| Operation               | Physical | Reserved |
|-------------------------|----------|----------|
| Reserve q               |    0     |    +q    |
| Cancel / expire         |    0     |    -q    |
| Fulfill                 |   -q     |    -q    |
"""

from __future__ import annotations

from app.db import DBConn
from app.errors import InvalidRequest, InvalidTransition, NotFound
from app.services import audit, idempotency
from app.services.common import User, inventory_out, require_positive_quantity, reservation_out
from app.services.inventory import INVENTORY_COLUMNS, raise_unavailable

MIN_TTL_MINUTES = 1
MAX_TTL_MINUTES = 7 * 24 * 60

# The stock check IS the update. PostgreSQL row-locks the inventory row; a
# concurrent transaction blocks, then re-evaluates the WHERE clause against the
# committed row, so two requests can never both take the last unit.
RESERVE_SQL = f"""
UPDATE inventory
SET reserved_quantity = reserved_quantity + %(quantity)s, updated_at = now()
WHERE product_id = %(product_id)s
  AND on_hand_quantity - reserved_quantity >= %(quantity)s
RETURNING {INVENTORY_COLUMNS}
"""

# action -> (on_hand factor, reserved factor, terminal status)
TRANSITIONS = {
    "cancel": (0, -1, "cancelled"),
    "expire": (0, -1, "expired"),
    "fulfill": (-1, -1, "fulfilled"),
}


def _validate_create(product_id: object, quantity: object, order_reference: object, ttl_minutes: object) -> dict:
    if not isinstance(product_id, int) or isinstance(product_id, bool) or product_id <= 0:
        raise InvalidRequest("product_id must be a positive whole number", field="product_id")
    require_positive_quantity(quantity)
    if not isinstance(order_reference, str) or not order_reference.strip() or len(order_reference.strip()) > 100:
        raise InvalidRequest("order_reference must be 1-100 characters", field="order_reference")
    if (
        not isinstance(ttl_minutes, int)
        or isinstance(ttl_minutes, bool)
        or not MIN_TTL_MINUTES <= ttl_minutes <= MAX_TTL_MINUTES
    ):
        raise InvalidRequest(
            f"ttl_minutes must be between {MIN_TTL_MINUTES} and {MAX_TTL_MINUTES}", field="ttl_minutes"
        )
    return {
        "product_id": product_id,
        "quantity": quantity,
        "order_reference": order_reference.strip(),
        "ttl_minutes": ttl_minutes,
    }


def create_reservation(
    conn: DBConn,
    actor: User,
    *,
    product_id: int,
    quantity: int,
    order_reference: str,
    ttl_minutes: int = 30,
) -> dict:
    params = _validate_create(product_id, quantity, order_reference, ttl_minutes)

    with conn.transaction():
        inv = conn.execute(RESERVE_SQL, params).fetchone()
        if inv is None:
            raise_unavailable(conn, product_id, "Not enough available stock", requested_quantity=quantity)
        assert inv is not None

        reservation = conn.execute(
            """
            INSERT INTO reservations (product_id, user_id, order_reference, quantity, expires_at)
            VALUES (%s, %s, %s, %s, now() + make_interval(mins => %s))
            RETURNING *
            """,
            (product_id, actor.id, params["order_reference"], quantity, ttl_minutes),
        ).fetchone()
        assert reservation is not None

        audit.record(
            conn,
            actor_id=actor.id,
            action="reserve",
            product_id=product_id,
            reservation_id=reservation["id"],
            on_hand_delta=0,
            reserved_delta=quantity,
            inventory_after=inv,
            reason=f"Reserved for order {params['order_reference']}",
        )

    return {"reservation": reservation_out(reservation), "inventory": inventory_out(inv)}


def create_reservation_idempotent(
    conn: DBConn,
    actor: User,
    idempotency_key: str | None,
    *,
    product_id: int,
    quantity: int,
    order_reference: str,
    ttl_minutes: int = 30,
) -> idempotency.IdempotentResult:
    key = idempotency.validate_key(idempotency_key)
    payload = _validate_create(product_id, quantity, order_reference, ttl_minutes)
    return idempotency.run_idempotent(
        conn,
        user_id=actor.id,
        key=key,
        scope="reservations.create",
        payload=payload,
        operation=lambda: (201, create_reservation(conn, actor, **payload)),
    )


def _can_access(actor: User | None, reservation) -> bool:
    if actor is None:  # system actor (expiry sweeper)
        return True
    return actor.is_manager or reservation["user_id"] == actor.id


def _transition(conn: DBConn, actor: User | None, reservation_id: int, action: str, reason: str | None = None) -> dict:
    on_hand_factor, reserved_factor, new_status = TRANSITIONS[action]

    with conn.transaction():
        # Lock the reservation row. A competing transition on the same
        # reservation waits here and then sees the updated status.
        res = conn.execute(
            """
            SELECT r.*, (r.expires_at <= now()) AS is_past_due
            FROM reservations r
            WHERE r.id = %s
            FOR UPDATE
            """,
            (reservation_id,),
        ).fetchone()

        # Ownership: someone else's reservation is indistinguishable from a missing one.
        if res is None or not _can_access(actor, res):
            raise NotFound("Reservation not found", reservation_id=reservation_id)
        if res["status"] != "active":
            raise InvalidTransition(
                f"Reservation is already {res['status']}",
                reservation_id=reservation_id,
                current_status=res["status"],
                attempted=action,
            )
        if action == "fulfill" and res["is_past_due"]:
            raise InvalidTransition(
                "Reservation has passed its expiry time and cannot be fulfilled",
                reservation_id=reservation_id,
                current_status=res["status"],
                attempted=action,
            )
        if action == "expire" and not res["is_past_due"]:
            raise InvalidTransition(
                "Reservation has not reached its expiry time",
                reservation_id=reservation_id,
                current_status=res["status"],
                attempted=action,
            )

        quantity = res["quantity"]
        on_hand_delta, reserved_delta = on_hand_factor * quantity, reserved_factor * quantity

        inv = conn.execute(
            f"""
            UPDATE inventory
            SET on_hand_quantity = on_hand_quantity + %s,
                reserved_quantity = reserved_quantity + %s,
                updated_at = now()
            WHERE product_id = %s
            RETURNING {INVENTORY_COLUMNS}
            """,
            (on_hand_delta, reserved_delta, res["product_id"]),
        ).fetchone()
        assert inv is not None

        # Status guard is redundant with the lock above; it is a cheap backstop.
        updated = conn.execute(
            """
            UPDATE reservations
            SET status = %s, closed_at = now(), updated_at = now()
            WHERE id = %s AND status = 'active'
            RETURNING *
            """,
            (new_status, reservation_id),
        ).fetchone()
        if updated is None:  # pragma: no cover - unreachable while the row lock is held
            raise InvalidTransition("Reservation changed concurrently", reservation_id=reservation_id)

        audit.record(
            conn,
            actor_id=actor.id if actor else None,
            action=action,
            product_id=res["product_id"],
            reservation_id=reservation_id,
            on_hand_delta=on_hand_delta,
            reserved_delta=reserved_delta,
            inventory_after=inv,
            reason=reason,
        )

    return {"reservation": reservation_out(updated), "inventory": inventory_out(inv)}


def cancel(conn: DBConn, actor: User, reservation_id: int, reason: str | None = None) -> dict:
    return _transition(conn, actor, reservation_id, "cancel", reason or "Cancelled")


def fulfill(conn: DBConn, actor: User, reservation_id: int, reason: str | None = None) -> dict:
    return _transition(conn, actor, reservation_id, "fulfill", reason or "Fulfilled")


def expire_due(conn: DBConn, *, limit: int = 500) -> int:
    """Expire active reservations past their expiry time. Each one is its own transaction."""
    due_ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM reservations WHERE status = 'active' AND expires_at <= now() ORDER BY expires_at LIMIT %s",
            (limit,),
        ).fetchall()
    ]
    expired = 0
    for reservation_id in due_ids:
        try:
            _transition(conn, None, reservation_id, "expire", "Expired")
            expired += 1
        except InvalidTransition:
            pass  # cancelled/fulfilled concurrently: the lock + status check made that safe
    return expired


def get_reservation(conn: DBConn, actor: User, reservation_id: int) -> dict:
    row = conn.execute(
        """
        SELECT r.*, u.username, p.sku
        FROM reservations r JOIN users u ON u.id = r.user_id JOIN products p ON p.id = r.product_id
        WHERE r.id = %s
        """,
        (reservation_id,),
    ).fetchone()
    if row is None or not _can_access(actor, row):
        raise NotFound("Reservation not found", reservation_id=reservation_id)
    return reservation_out(row)


def list_reservations(conn: DBConn, actor: User, *, status: str | None = None, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        """
        SELECT r.*, u.username, p.sku
        FROM reservations r JOIN users u ON u.id = r.user_id JOIN products p ON p.id = r.product_id
        WHERE (%(is_manager)s OR r.user_id = %(user_id)s)
          AND (%(status)s::text IS NULL OR r.status = %(status)s)
        ORDER BY r.id DESC
        LIMIT %(limit)s
        """,
        {"is_manager": actor.is_manager, "user_id": actor.id, "status": status, "limit": limit},
    ).fetchall()
    return [reservation_out(r) for r in rows]
