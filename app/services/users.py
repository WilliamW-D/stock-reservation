from __future__ import annotations

from app.db import DBConn
from app.errors import InvalidRequest
from app.security import DEFAULT_ITERATIONS, hash_password, verify_password
from app.services.common import User


def _to_user(row: dict) -> User:
    return User(id=row["id"], username=row["username"], role=row["role"])


def create_user(conn: DBConn, username: str, password: str, role: str, *, iterations: int = DEFAULT_ITERATIONS) -> User:
    if role not in ("employee", "manager"):
        raise InvalidRequest("role must be 'employee' or 'manager'", field="role")
    row = conn.execute(
        "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s) RETURNING id, username, role",
        (username, hash_password(password, iterations=iterations), role),
    ).fetchone()
    assert row is not None
    return _to_user(row)


def get_user(conn: DBConn, user_id: int) -> User | None:
    row = conn.execute("SELECT id, username, role FROM users WHERE id = %s", (user_id,)).fetchone()
    return _to_user(row) if row else None


def get_user_by_username(conn: DBConn, username: str) -> User | None:
    row = conn.execute("SELECT id, username, role FROM users WHERE username = %s", (username,)).fetchone()
    return _to_user(row) if row else None


def authenticate(conn: DBConn, username: str, password: str) -> User | None:
    row = conn.execute(
        "SELECT id, username, role, password_hash FROM users WHERE username = %s", (username,)
    ).fetchone()
    if row is None or not verify_password(password, row["password_hash"]):
        return None
    return _to_user(row)


MAX_FAILED_ATTEMPTS = 5
RATE_LIMIT_WINDOW_MINUTES = 5


def check_login_rate_limit(
    conn: DBConn,
    ip_address: str,
    username: str,
    *,
    max_attempts: int = MAX_FAILED_ATTEMPTS,
    window_minutes: int = RATE_LIMIT_WINDOW_MINUTES,
) -> None:
    recent_failures = conn.execute(
        """
        SELECT count(*) AS n
        FROM login_attempts
        WHERE (ip_address = %s OR username = %s)
          AND success = FALSE
          AND attempted_at >= now() - make_interval(mins => %s)
        """,
        (ip_address, username, window_minutes),
    ).fetchone()
    failure_count = recent_failures["n"] if recent_failures else 0
    if failure_count >= max_attempts:
        from app.errors import TooManyRequests

        raise TooManyRequests(
            f"Too many failed login attempts. Try again in {window_minutes} minutes.",
            max_attempts=max_attempts,
            window_minutes=window_minutes,
        )


def record_login_attempt(conn: DBConn, ip_address: str, username: str, success: bool) -> None:
    conn.execute(
        """
        INSERT INTO login_attempts (ip_address, username, success)
        VALUES (%s, %s, %s)
        """,
        (ip_address, username, success),
    )


def authenticate_with_rate_limit(
    conn: DBConn,
    username: str,
    password: str,
    ip_address: str,
    *,
    max_attempts: int = MAX_FAILED_ATTEMPTS,
    window_minutes: int = RATE_LIMIT_WINDOW_MINUTES,
) -> User | None:
    check_login_rate_limit(conn, ip_address, username, max_attempts=max_attempts, window_minutes=window_minutes)
    user = authenticate(conn, username, password)
    record_login_attempt(conn, ip_address, username, success=user is not None)
    return user
