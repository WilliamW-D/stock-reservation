"""Reservations: create (multi-product, deadlock-safe), cancel, fulfill, partial fulfill, expire.

| Operation               | Physical | Reserved |
|-------------------------|----------|----------|
| Reserve q               |    0     |    +q    |
| Cancel / expire         |    0     |    -q    |
| Fulfill                 |   -q     |    -q    |
"""

from __future__ import annotations

from typing import Any

from app.db import DBConn
from app.errors import InsufficientStock, InvalidRequest, InvalidTransition, NotFound
from app.services import audit, idempotency
from app.services.common import (
    User,
    inventory_out,
    require_positive_quantity,
    reservation_line_out,
    reservation_out,
)
from app.services.inventory import INVENTORY_COLUMNS

MIN_TTL_MINUTES = 1
MAX_TTL_MINUTES = 7 * 24 * 60

RESERVE_SQL = f"""
UPDATE inventory
SET reserved_quantity = reserved_quantity + %(quantity)s, updated_at = now()
WHERE product_id = %(product_id)s
  AND on_hand_quantity - reserved_quantity >= %(quantity)s
RETURNING {INVENTORY_COLUMNS}
"""


def _validate_items(items: list[dict[str, Any]]) -> list[dict[str, int]]:
    if not isinstance(items, list) or len(items) == 0:
        raise InvalidRequest("items must be a non-empty list", field="items")
    consolidated: dict[int, int] = {}
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise InvalidRequest(f"items[{i}] must be an object", field="items")
        pid = it.get("product_id")
        qty = it.get("quantity")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise InvalidRequest(f"items[{i}].product_id must be a positive integer", field="product_id")
        require_positive_quantity(qty, field=f"items[{i}].quantity")
        assert isinstance(qty, int)
        consolidated[pid] = consolidated.get(pid, 0) + qty

    # Return canonical sorted list of items by product_id
    return [{"product_id": pid, "quantity": consolidated[pid]} for pid in sorted(consolidated.keys())]


def _validate_create_params(
    *,
    items: list[dict[str, Any]] | None = None,
    product_id: int | None = None,
    quantity: int | None = None,
    order_reference: object,
    ttl_minutes: object,
) -> tuple[list[dict[str, int]], str, int]:
    if items is None:
        if product_id is None or quantity is None:
            raise InvalidRequest("Either items list or product_id and quantity must be provided")
        items = [{"product_id": product_id, "quantity": quantity}]

    validated_items = _validate_items(items)

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
    return validated_items, order_reference.strip(), ttl_minutes


def create_reservation(
    conn: DBConn,
    actor: User,
    *,
    items: list[dict[str, Any]] | None = None,
    product_id: int | None = None,
    quantity: int | None = None,
    order_reference: str,
    ttl_minutes: int = 30,
) -> dict:
    validated_items, order_ref, ttl = _validate_create_params(
        items=items,
        product_id=product_id,
        quantity=quantity,
        order_reference=order_reference,
        ttl_minutes=ttl_minutes,
    )

    # 1. Acquire row locks in strictly sorted product_id order to eliminate deadlocks.
    sorted_pids = [it["product_id"] for it in validated_items]

    with conn.transaction():
        locked_rows = conn.execute(
            f"""
            SELECT {INVENTORY_COLUMNS}
            FROM inventory
            WHERE product_id = ANY(%s)
            ORDER BY product_id
            FOR UPDATE
            """,
            (sorted_pids,),
        ).fetchall()

        found_by_id = {row["product_id"]: row for row in locked_rows}

        # Check product existence
        for it in validated_items:
            pid = it["product_id"]
            if pid not in found_by_id:
                raise NotFound("Product not found", product_id=pid)

        # Check stock across all items before applying any changes (all-or-nothing guarantee)
        shortfalls = []
        for it in validated_items:
            pid = it["product_id"]
            curr = found_by_id[pid]
            if curr["available_quantity"] < it["quantity"]:
                shortfalls.append(
                    {
                        "product_id": pid,
                        "requested_quantity": it["quantity"],
                        "available_quantity": curr["available_quantity"],
                        "on_hand_quantity": curr["on_hand_quantity"],
                        "reserved_quantity": curr["reserved_quantity"],
                    }
                )

        if shortfalls:
            first = shortfalls[0]
            raise InsufficientStock(
                "Not enough available stock for requested items",
                product_id=first["product_id"],
                requested_quantity=first["requested_quantity"],
                available_quantity=first["available_quantity"],
                on_hand_quantity=first["on_hand_quantity"],
                reserved_quantity=first["reserved_quantity"],
                shortfalls=shortfalls,
            )

        # 2. Apply atomic updates to inventory rows in sorted order
        updated_inventories = []
        for it in validated_items:
            inv = conn.execute(RESERVE_SQL, it).fetchone()
            assert inv is not None
            updated_inventories.append(inv)

        # 3. Create parent reservation
        single_pid = validated_items[0]["product_id"] if len(validated_items) == 1 else None
        total_qty = sum(it["quantity"] for it in validated_items)
        reservation = conn.execute(
            """
            INSERT INTO reservations (user_id, order_reference, expires_at, product_id, quantity)
            VALUES (%s, %s, now() + make_interval(mins => %s), %s, %s)
            RETURNING *
            """,
            (actor.id, order_ref, ttl, single_pid, total_qty),
        ).fetchone()
        assert reservation is not None
        res_id = reservation["id"]

        # 4. Create reservation lines and audit events
        lines = []
        for it, inv in zip(validated_items, updated_inventories, strict=True):
            line = conn.execute(
                """
                INSERT INTO reservation_lines (reservation_id, product_id, quantity)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (res_id, it["product_id"], it["quantity"]),
            ).fetchone()
            assert line is not None
            lines.append(line)

            audit.record(
                conn,
                actor_id=actor.id,
                action="reserve",
                product_id=it["product_id"],
                reservation_id=res_id,
                reservation_line_id=line["id"],
                on_hand_delta=0,
                reserved_delta=it["quantity"],
                inventory_after=inv,
                reason=f"Reserved for order {order_ref}",
            )

    formatted_lines = [reservation_line_out(line) for line in lines]
    res_out = reservation_out(reservation, formatted_lines)
    # For single-item requests, also expose inventory as single dict or list of dicts
    inv_out = (
        inventory_out(updated_inventories[0])
        if len(updated_inventories) == 1
        else [inventory_out(inv) for inv in updated_inventories]
    )
    return {"reservation": res_out, "inventory": inv_out}


def create_reservation_idempotent(
    conn: DBConn,
    actor: User,
    idempotency_key: str | None,
    *,
    items: list[dict[str, Any]] | None = None,
    product_id: int | None = None,
    quantity: int | None = None,
    order_reference: str,
    ttl_minutes: int = 30,
) -> idempotency.IdempotentResult:
    key = idempotency.validate_key(idempotency_key)
    validated_items, order_ref, ttl = _validate_create_params(
        items=items,
        product_id=product_id,
        quantity=quantity,
        order_reference=order_reference,
        ttl_minutes=ttl_minutes,
    )
    payload = {
        "items": validated_items,
        "order_reference": order_ref,
        "ttl_minutes": ttl,
    }
    return idempotency.run_idempotent(
        conn,
        user_id=actor.id,
        key=key,
        scope="reservations.create",
        payload=payload,
        operation=lambda: (
            201,
            create_reservation(
                conn,
                actor,
                items=validated_items,
                order_reference=order_ref,
                ttl_minutes=ttl,
            ),
        ),
    )


def _can_access(actor: User | None, reservation: dict[str, Any]) -> bool:
    if actor is None:
        return True
    return actor.is_manager or reservation["user_id"] == actor.id


def _fetch_lines(conn: DBConn, reservation_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT rl.*, p.sku, p.name, p.unit
        FROM reservation_lines rl
        JOIN products p ON p.id = rl.product_id
        WHERE rl.reservation_id = %s
        ORDER BY rl.id
        """,
        (reservation_id,),
    ).fetchall()
    return [reservation_line_out(r) for r in rows]


def get_reservation(conn: DBConn, actor: User, reservation_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT r.*, u.username
        FROM reservations r
        JOIN users u ON u.id = r.user_id
        WHERE r.id = %s
        """,
        (reservation_id,),
    ).fetchone()
    if row is None or not _can_access(actor, row):
        raise NotFound("Reservation not found", reservation_id=reservation_id)
    lines = _fetch_lines(conn, reservation_id)
    return reservation_out(row, lines)


def list_reservations(
    conn: DBConn, actor: User, *, status: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT r.*, u.username
        FROM reservations r
        JOIN users u ON u.id = r.user_id
        WHERE (%(is_manager)s OR r.user_id = %(user_id)s)
          AND (%(status)s::text IS NULL OR r.status = %(status)s)
        ORDER BY r.id DESC
        LIMIT %(limit)s
        """,
        {"is_manager": actor.is_manager, "user_id": actor.id, "status": status, "limit": limit},
    ).fetchall()

    result = []
    for r in rows:
        lines = _fetch_lines(conn, r["id"])
        result.append(reservation_out(r, lines))
    return result


def cancel(conn: DBConn, actor: User, reservation_id: int, reason: str | None = None) -> dict[str, Any]:
    with conn.transaction():
        res = conn.execute(
            """
            SELECT r.*
            FROM reservations r
            WHERE r.id = %s
            FOR UPDATE
            """,
            (reservation_id,),
        ).fetchone()

        if res is None or not _can_access(actor, res):
            raise NotFound("Reservation not found", reservation_id=reservation_id)
        if res["status"] not in ("active", "partially_fulfilled"):
            raise InvalidTransition(
                f"Reservation is already {res['status']}",
                reservation_id=reservation_id,
                current_status=res["status"],
                attempted="cancel",
            )

        # Lock active lines
        lines = conn.execute(
            """
            SELECT * FROM reservation_lines
            WHERE reservation_id = %s AND status IN ('active', 'partially_fulfilled')
            ORDER BY product_id
            FOR UPDATE
            """,
            (reservation_id,),
        ).fetchall()

        # Sort product IDs to lock inventory rows in sorted order
        pids = sorted({line["product_id"] for line in lines})
        if pids:
            conn.execute(
                f"SELECT {INVENTORY_COLUMNS} FROM inventory WHERE product_id = ANY(%s) ORDER BY product_id FOR UPDATE",
                (pids,),
            )

        updated_invs = []
        for line in lines:
            remaining = line["quantity"] - line["fulfilled_quantity"]
            if remaining > 0:
                inv = conn.execute(
                    f"""
                    UPDATE inventory
                    SET reserved_quantity = reserved_quantity - %s, updated_at = now()
                    WHERE product_id = %s
                    RETURNING {INVENTORY_COLUMNS}
                    """,
                    (remaining, line["product_id"]),
                ).fetchone()
                assert inv is not None
                updated_invs.append(inv)

                audit.record(
                    conn,
                    actor_id=actor.id if actor else None,
                    action="cancel",
                    product_id=line["product_id"],
                    reservation_id=reservation_id,
                    reservation_line_id=line["id"],
                    on_hand_delta=0,
                    reserved_delta=-remaining,
                    inventory_after=inv,
                    reason=reason or "Cancelled",
                )

            conn.execute(
                """
                UPDATE reservation_lines
                SET status = 'cancelled', closed_at = now(), updated_at = now()
                WHERE id = %s
                """,
                (line["id"],),
            )

        updated_res = conn.execute(
            """
            UPDATE reservations
            SET status = 'cancelled', closed_at = now(), updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (reservation_id,),
        ).fetchone()
        assert updated_res is not None

    all_lines = _fetch_lines(conn, reservation_id)
    inv_out = (
        inventory_out(updated_invs[0])
        if len(updated_invs) == 1
        else [inventory_out(i) for i in updated_invs]
        if updated_invs
        else {}
    )
    return {"reservation": reservation_out(updated_res, all_lines), "inventory": inv_out}


def fulfill(conn: DBConn, actor: User, reservation_id: int, reason: str | None = None) -> dict[str, Any]:
    with conn.transaction():
        res = conn.execute(
            """
            SELECT r.*, (r.expires_at <= now()) AS is_past_due
            FROM reservations r
            WHERE r.id = %s
            FOR UPDATE
            """,
            (reservation_id,),
        ).fetchone()

        if res is None or not _can_access(actor, res):
            raise NotFound("Reservation not found", reservation_id=reservation_id)
        if res["status"] not in ("active", "partially_fulfilled"):
            raise InvalidTransition(
                f"Reservation is already {res['status']}",
                reservation_id=reservation_id,
                current_status=res["status"],
                attempted="fulfill",
            )
        if res["is_past_due"]:
            raise InvalidTransition(
                "Reservation has passed its expiry time and cannot be fulfilled",
                reservation_id=reservation_id,
                current_status=res["status"],
                attempted="fulfill",
            )

        lines = conn.execute(
            """
            SELECT * FROM reservation_lines
            WHERE reservation_id = %s AND status IN ('active', 'partially_fulfilled')
            ORDER BY product_id
            FOR UPDATE
            """,
            (reservation_id,),
        ).fetchall()

        pids = sorted({line["product_id"] for line in lines})
        if pids:
            conn.execute(
                f"SELECT {INVENTORY_COLUMNS} FROM inventory WHERE product_id = ANY(%s) ORDER BY product_id FOR UPDATE",
                (pids,),
            )

        updated_invs = []
        for line in lines:
            remaining = line["quantity"] - line["fulfilled_quantity"]
            if remaining > 0:
                inv = conn.execute(
                    f"""
                    UPDATE inventory
                    SET on_hand_quantity = on_hand_quantity - %s,
                        reserved_quantity = reserved_quantity - %s,
                        updated_at = now()
                    WHERE product_id = %s
                    RETURNING {INVENTORY_COLUMNS}
                    """,
                    (remaining, remaining, line["product_id"]),
                ).fetchone()
                assert inv is not None
                updated_invs.append(inv)

                audit.record(
                    conn,
                    actor_id=actor.id if actor else None,
                    action="fulfill",
                    product_id=line["product_id"],
                    reservation_id=reservation_id,
                    reservation_line_id=line["id"],
                    on_hand_delta=-remaining,
                    reserved_delta=-remaining,
                    inventory_after=inv,
                    reason=reason or "Fulfilled",
                )

            conn.execute(
                """
                UPDATE reservation_lines
                SET fulfilled_quantity = quantity, status = 'fulfilled', closed_at = now(), updated_at = now()
                WHERE id = %s
                """,
                (line["id"],),
            )

        updated_res = conn.execute(
            """
            UPDATE reservations
            SET status = 'fulfilled', closed_at = now(), updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (reservation_id,),
        ).fetchone()
        assert updated_res is not None

    all_lines = _fetch_lines(conn, reservation_id)
    inv_out = (
        inventory_out(updated_invs[0])
        if len(updated_invs) == 1
        else [inventory_out(i) for i in updated_invs]
        if updated_invs
        else {}
    )
    return {"reservation": reservation_out(updated_res, all_lines), "inventory": inv_out}


def fulfill_line(
    conn: DBConn,
    actor: User,
    reservation_id: int,
    line_id: int,
    quantity: int | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    with conn.transaction():
        res = conn.execute(
            """
            SELECT r.*, (r.expires_at <= now()) AS is_past_due
            FROM reservations r
            WHERE r.id = %s
            FOR UPDATE
            """,
            (reservation_id,),
        ).fetchone()

        if res is None or not _can_access(actor, res):
            raise NotFound("Reservation not found", reservation_id=reservation_id)
        if res["status"] not in ("active", "partially_fulfilled"):
            raise InvalidTransition(
                f"Reservation is already {res['status']}",
                reservation_id=reservation_id,
                current_status=res["status"],
                attempted="fulfill",
            )
        if res["is_past_due"]:
            raise InvalidTransition(
                "Reservation has passed its expiry time and cannot be fulfilled",
                reservation_id=reservation_id,
                current_status=res["status"],
                attempted="fulfill",
            )

        line = conn.execute(
            """
            SELECT * FROM reservation_lines
            WHERE id = %s AND reservation_id = %s
            FOR UPDATE
            """,
            (line_id, reservation_id),
        ).fetchone()

        if line is None:
            raise NotFound("Reservation line not found", line_id=line_id)
        if line["status"] not in ("active", "partially_fulfilled"):
            raise InvalidTransition(
                f"Reservation line is already {line['status']}",
                line_id=line_id,
                current_status=line["status"],
                attempted="fulfill",
            )

        remaining = line["quantity"] - line["fulfilled_quantity"]
        fulfill_qty = remaining if quantity is None else quantity
        require_positive_quantity(fulfill_qty, field="quantity")
        if fulfill_qty > remaining:
            raise InvalidRequest(
                f"Requested quantity {fulfill_qty} exceeds unfulfilled quantity {remaining}",
                field="quantity",
            )

        # Lock inventory row
        inv = conn.execute(
            f"""
            UPDATE inventory
            SET on_hand_quantity = on_hand_quantity - %s,
                reserved_quantity = reserved_quantity - %s,
                updated_at = now()
            WHERE product_id = %s
            RETURNING {INVENTORY_COLUMNS}
            """,
            (fulfill_qty, fulfill_qty, line["product_id"]),
        ).fetchone()
        assert inv is not None

        new_fulfilled = line["fulfilled_quantity"] + fulfill_qty
        new_line_status = "fulfilled" if new_fulfilled == line["quantity"] else "partially_fulfilled"
        closed_at_clause = "closed_at = now()," if new_line_status == "fulfilled" else ""

        conn.execute(
            f"""
            UPDATE reservation_lines
            SET fulfilled_quantity = %s, status = %s, {closed_at_clause} updated_at = now()
            WHERE id = %s
            """,
            (new_fulfilled, new_line_status, line_id),
        )

        audit.record(
            conn,
            actor_id=actor.id if actor else None,
            action="fulfill",
            product_id=line["product_id"],
            reservation_id=reservation_id,
            reservation_line_id=line_id,
            on_hand_delta=-fulfill_qty,
            reserved_delta=-fulfill_qty,
            inventory_after=inv,
            reason=reason or "Line fulfilled",
        )

        # Check all lines to determine parent reservation status
        all_lines_rows = conn.execute(
            "SELECT status FROM reservation_lines WHERE reservation_id = %s",
            (reservation_id,),
        ).fetchall()

        all_fulfilled = all(lr["status"] == "fulfilled" for lr in all_lines_rows)
        new_res_status = "fulfilled" if all_fulfilled else "partially_fulfilled"
        res_closed_clause = "closed_at = now()," if new_res_status == "fulfilled" else ""

        updated_res = conn.execute(
            f"""
            UPDATE reservations
            SET status = %s, {res_closed_clause} updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (new_res_status, reservation_id),
        ).fetchone()
        assert updated_res is not None

    all_lines = _fetch_lines(conn, reservation_id)
    return {"reservation": reservation_out(updated_res, all_lines), "inventory": inventory_out(inv)}


def expire_due(conn: DBConn, *, limit: int = 500) -> int:
    expired = 0
    for _ in range(limit):
        try:
            with conn.transaction():
                res = conn.execute(
                    """
                    SELECT * FROM reservations
                    WHERE status IN ('active', 'partially_fulfilled') AND expires_at <= now()
                    ORDER BY expires_at
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """
                ).fetchone()
                if res is None:
                    break

                reservation_id = res["id"]
                lines = conn.execute(
                    """
                    SELECT * FROM reservation_lines
                    WHERE reservation_id = %s AND status IN ('active', 'partially_fulfilled')
                    ORDER BY product_id
                    FOR UPDATE
                    """,
                    (reservation_id,),
                ).fetchall()

                pids = sorted({line["product_id"] for line in lines})
                if pids:
                    conn.execute(
                        f"SELECT {INVENTORY_COLUMNS} FROM inventory WHERE product_id = ANY(%s) ORDER BY product_id FOR UPDATE",
                        (pids,),
                    )

                for line in lines:
                    remaining = line["quantity"] - line["fulfilled_quantity"]
                    if remaining > 0:
                        inv = conn.execute(
                            f"""
                            UPDATE inventory
                            SET reserved_quantity = reserved_quantity - %s, updated_at = now()
                            WHERE product_id = %s
                            RETURNING {INVENTORY_COLUMNS}
                            """,
                            (remaining, line["product_id"]),
                        ).fetchone()
                        assert inv is not None

                        audit.record(
                            conn,
                            actor_id=None,
                            action="expire",
                            product_id=line["product_id"],
                            reservation_id=reservation_id,
                            reservation_line_id=line["id"],
                            on_hand_delta=0,
                            reserved_delta=-remaining,
                            inventory_after=inv,
                            reason="Expired",
                        )

                    conn.execute(
                        """
                        UPDATE reservation_lines
                        SET status = 'expired', closed_at = now(), updated_at = now()
                        WHERE id = %s
                        """,
                        (line["id"],),
                    )

                conn.execute(
                    """
                    UPDATE reservations
                    SET status = 'expired', closed_at = now(), updated_at = now()
                    WHERE id = %s
                    """,
                    (reservation_id,),
                )
            expired += 1
        except InvalidTransition:
            pass
    return expired
