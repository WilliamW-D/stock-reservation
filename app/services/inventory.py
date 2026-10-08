from __future__ import annotations

from typing import Any

from app.db import DBConn
from app.errors import InsufficientStock, InvalidRequest, NotFound
from app.services import audit
from app.services.common import MAX_QUANTITY, User, inventory_out, require_manager, require_positive_quantity

INVENTORY_COLUMNS = "product_id, on_hand_quantity, reserved_quantity, available_quantity"


def create_product(conn: DBConn, actor: User, *, sku: str, name: str, unit: str) -> dict:
    require_manager(actor)
    sku, name, unit = sku.strip(), name.strip(), unit.strip()
    if not sku or not name or not unit:
        raise InvalidRequest("sku, name and unit are required")
    with conn.transaction():
        product = conn.execute(
            "INSERT INTO products (sku, name, unit) VALUES (%s, %s, %s) ON CONFLICT (sku) DO NOTHING "
            "RETURNING id, sku, name, unit",
            (sku, name, unit),
        ).fetchone()
        if product is None:
            raise InvalidRequest("A product with this SKU already exists", field="sku")
        conn.execute("INSERT INTO inventory (product_id) VALUES (%s)", (product["id"],))
    return get_inventory(conn, product["id"])


def get_inventory(conn: DBConn, product_id: int) -> dict:
    row = conn.execute(
        f"""
        SELECT i.{INVENTORY_COLUMNS.replace(", ", ", i.")}, p.sku, p.name, p.unit
        FROM inventory i JOIN products p ON p.id = i.product_id
        WHERE i.product_id = %s
        """,
        (product_id,),
    ).fetchone()
    if row is None:
        raise NotFound("Product not found", product_id=product_id)
    return inventory_out(row)


def list_inventory(conn: DBConn) -> list[dict]:
    rows = conn.execute(
        f"""
        SELECT i.{INVENTORY_COLUMNS.replace(", ", ", i.")}, p.sku, p.name, p.unit
        FROM inventory i JOIN products p ON p.id = i.product_id
        ORDER BY p.sku
        """
    ).fetchall()
    return [inventory_out(r) for r in rows]


def raise_unavailable(conn: DBConn, product_id: int, message: str, **details: Any) -> None:
    """A conditional UPDATE matched no row: distinguish unknown product from insufficient stock."""
    current = conn.execute(f"SELECT {INVENTORY_COLUMNS} FROM inventory WHERE product_id = %s", (product_id,)).fetchone()
    if current is None:
        raise NotFound("Product not found", product_id=product_id)
    raise InsufficientStock(
        message,
        product_id=product_id,
        on_hand_quantity=current["on_hand_quantity"],
        reserved_quantity=current["reserved_quantity"],
        available_quantity=current["available_quantity"],
        **details,
    )


def receive(conn: DBConn, actor: User, product_id: int, quantity: int, reason: str | None = None) -> dict:
    """Delivery: physical +quantity, reserved unchanged."""
    require_manager(actor)
    require_positive_quantity(quantity)
    with conn.transaction():
        inv = conn.execute(
            f"""
            UPDATE inventory
            SET on_hand_quantity = on_hand_quantity + %s, updated_at = now()
            WHERE product_id = %s
            RETURNING {INVENTORY_COLUMNS}
            """,
            (quantity, product_id),
        ).fetchone()
        if inv is None:
            raise NotFound("Product not found", product_id=product_id)
        audit.record(
            conn,
            actor_id=actor.id,
            action="receive",
            product_id=product_id,
            on_hand_delta=quantity,
            reserved_delta=0,
            inventory_after=inv,
            reason=reason or "Delivery received",
        )
    return inventory_out(inv)


def adjust(conn: DBConn, actor: User, product_id: int, delta: int, reason: str) -> dict:
    """Manual correction (e.g. spoilage = negative delta).

    The stock check is part of the UPDATE: physical stock may never drop below
    what is already promised to reservations.
    """
    require_manager(actor)
    if not isinstance(delta, int) or isinstance(delta, bool) or delta == 0:
        raise InvalidRequest("delta must be a non-zero whole number", field="delta")
    if abs(delta) > MAX_QUANTITY:
        raise InvalidRequest(f"delta must be at most {MAX_QUANTITY} in magnitude", field="delta")
    if not reason or not reason.strip():
        raise InvalidRequest("A reason is required for adjustments", field="reason")
    with conn.transaction():
        inv = conn.execute(
            f"""
            UPDATE inventory
            SET on_hand_quantity = on_hand_quantity + %(delta)s, updated_at = now()
            WHERE product_id = %(product_id)s
              AND on_hand_quantity + %(delta)s >= reserved_quantity
            RETURNING {INVENTORY_COLUMNS}
            """,
            {"delta": delta, "product_id": product_id},
        ).fetchone()
        if inv is None:
            raise_unavailable(
                conn,
                product_id,
                "Adjustment would leave physical stock below reserved stock",
                requested_delta=delta,
            )
        assert inv is not None
        audit.record(
            conn,
            actor_id=actor.id,
            action="adjust",
            product_id=product_id,
            on_hand_delta=delta,
            reserved_delta=0,
            inventory_after=inv,
            reason=reason.strip(),
        )
    return inventory_out(inv)
