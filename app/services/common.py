from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.errors import InvalidRequest, PermissionDenied

MAX_QUANTITY = 1_000_000


@dataclass(frozen=True)
class User:
    id: int
    username: str
    role: str

    @property
    def is_manager(self) -> bool:
        return self.role == "manager"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "username": self.username, "role": self.role}


def require_manager(actor: User) -> None:
    if not actor.is_manager:
        raise PermissionDenied("Only managers can perform this action")


def require_positive_quantity(quantity: object, field: str = "quantity") -> int:
    # bool is a subclass of int in Python; reject it explicitly.
    if not isinstance(quantity, int) or isinstance(quantity, bool):
        raise InvalidRequest(f"{field} must be a whole number", field=field)
    if quantity <= 0:
        raise InvalidRequest(f"{field} must be greater than zero", field=field)
    if quantity > MAX_QUANTITY:
        raise InvalidRequest(f"{field} must be at most {MAX_QUANTITY}", field=field)
    return quantity


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def inventory_out(row: Mapping[str, Any]) -> dict[str, Any]:
    out = {
        "product_id": row["product_id"],
        "on_hand_quantity": row["on_hand_quantity"],
        "reserved_quantity": row["reserved_quantity"],
        "available_quantity": row["available_quantity"],
    }
    for key in ("sku", "name", "unit"):
        if key in row:
            out[key] = row[key]
    return out


def reservation_line_out(row: Mapping[str, Any]) -> dict[str, Any]:
    out = {
        "id": row["id"],
        "product_id": row["product_id"],
        "quantity": row["quantity"],
        "fulfilled_quantity": row.get("fulfilled_quantity", 0),
        "status": row["status"],
        "created_at": _iso(row.get("created_at")),
        "closed_at": _iso(row.get("closed_at")),
    }
    for key in ("sku", "name", "unit"):
        if key in row:
            out[key] = row[key]
    return out


def reservation_out(row: Mapping[str, Any], lines: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    out = {
        "id": row["id"],
        "user_id": row["user_id"],
        "order_reference": row["order_reference"],
        "status": row["status"],
        "expires_at": _iso(row["expires_at"]),
        "created_at": _iso(row["created_at"]),
        "closed_at": _iso(row["closed_at"]),
        "items": lines if lines is not None else [],
    }
    if "product_id" in row and row["product_id"] is not None:
        out["product_id"] = row["product_id"]
        out["quantity"] = row["quantity"]
    if "username" in row:
        out["username"] = row["username"]
    return out


def audit_out(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "actor_id": row["actor_id"],
        "actor_username": row.get("actor_username"),
        "action": row["action"],
        "product_id": row["product_id"],
        "sku": row.get("sku"),
        "reservation_id": row["reservation_id"],
        "reservation_line_id": row.get("reservation_line_id"),
        "on_hand_delta": row["on_hand_delta"],
        "reserved_delta": row["reserved_delta"],
        "on_hand_after": row["on_hand_after"],
        "reserved_after": row["reserved_after"],
        "reason": row["reason"],
        "created_at": _iso(row["created_at"]),
    }
