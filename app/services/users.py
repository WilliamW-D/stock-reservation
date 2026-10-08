from __future__ import annotations

import psycopg

from app.errors import InvalidRequest
from app.security import DEFAULT_ITERATIONS, hash_password, verify_password
from app.services.common import User


def _to_user(row) -> User:
    return User(id=row["id"], username=row["username"], role=row["role"])


def create_user(
    conn: psycopg.Connection, username: str, password: str, role: str, *, iterations: int = DEFAULT_ITERATIONS
) -> User:
    if role not in ("employee", "manager"):
        raise InvalidRequest("role must be 'employee' or 'manager'", field="role")
    row = conn.execute(
        "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s) RETURNING id, username, role",
        (username, hash_password(password, iterations=iterations), role),
    ).fetchone()
    return _to_user(row)


def get_user(conn: psycopg.Connection, user_id: int) -> User | None:
    row = conn.execute("SELECT id, username, role FROM users WHERE id = %s", (user_id,)).fetchone()
    return _to_user(row) if row else None


def get_user_by_username(conn: psycopg.Connection, username: str) -> User | None:
    row = conn.execute("SELECT id, username, role FROM users WHERE username = %s", (username,)).fetchone()
    return _to_user(row) if row else None


def authenticate(conn: psycopg.Connection, username: str, password: str) -> User | None:
    row = conn.execute(
        "SELECT id, username, role, password_hash FROM users WHERE username = %s", (username,)
    ).fetchone()
    if row is None or not verify_password(password, row["password_hash"]):
        return None
    return _to_user(row)
