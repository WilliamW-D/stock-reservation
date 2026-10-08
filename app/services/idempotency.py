"""Idempotent execution backed by a database uniqueness constraint (I5).

The key is claimed with ``INSERT … ON CONFLICT DO NOTHING`` *inside the same
transaction* as the work it protects:

* First request: the insert succeeds, the operation runs, the result is stored,
  everything commits together.
* Concurrent duplicate: its insert blocks on the primary-key index until the
  first transaction finishes. If that committed, the insert becomes a no-op and
  we read the stored result. If it rolled back, the duplicate claims the key and
  does the work itself. No "in progress" state is ever visible.
* Failed operation: the claim rolls back with everything else, so the key is
  not burned and a later retry can succeed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from app.db import DBConn
from app.errors import IdempotencyConflict, InvalidRequest

MAX_KEY_LENGTH = 255


@dataclass(frozen=True)
class IdempotentResult:
    status_code: int
    body: dict[str, Any]
    replayed: bool


def validate_key(key: str | None) -> str:
    if key is None or not key.strip():
        raise InvalidRequest("An Idempotency-Key header is required", field="Idempotency-Key")
    key = key.strip()
    if len(key) > MAX_KEY_LENGTH:
        raise InvalidRequest(f"Idempotency-Key must be at most {MAX_KEY_LENGTH} characters", field="Idempotency-Key")
    return key


def fingerprint(scope: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps({"scope": scope, "payload": payload}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


DEFAULT_TTL_HOURS = 24


def run_idempotent(
    conn: DBConn,
    *,
    user_id: int,
    key: str,
    scope: str,
    payload: dict[str, Any],
    operation: Callable[[], tuple[int, dict[str, Any]]],
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> IdempotentResult:
    key = validate_key(key)
    request_fingerprint = fingerprint(scope, payload)

    with conn.transaction():
        claimed = conn.execute(
            """
            INSERT INTO idempotency_records (user_id, idempotency_key, request_fingerprint, expires_at)
            VALUES (%s, %s, %s, now() + make_interval(hours => %s))
            ON CONFLICT (user_id, idempotency_key) DO NOTHING
            RETURNING 1
            """,
            (user_id, key, request_fingerprint, ttl_hours),
        ).fetchone()

        if claimed is None:
            existing = conn.execute(
                """
                SELECT request_fingerprint, response_status, response_body,
                       (expires_at <= now()) AS is_expired
                FROM idempotency_records
                WHERE user_id = %s AND idempotency_key = %s
                FOR UPDATE
                """,
                (user_id, key),
            ).fetchone()
            if existing is None or (existing["response_status"] is None and not existing["is_expired"]):
                raise IdempotencyConflict("Idempotency key is in an unexpected state; retry")

            # Expired key: treat as a fresh request (standard industry retention semantics)
            if existing["is_expired"]:
                status_code, body = operation()
                conn.execute(
                    """
                    UPDATE idempotency_records
                    SET request_fingerprint = %s, response_status = %s, response_body = %s,
                        created_at = now(), expires_at = now() + make_interval(hours => %s)
                    WHERE user_id = %s AND idempotency_key = %s
                    """,
                    (request_fingerprint, status_code, Jsonb(body), ttl_hours, user_id, key),
                )
                return IdempotentResult(status_code, body, replayed=False)

            if existing["request_fingerprint"] != request_fingerprint:
                raise IdempotencyConflict(
                    "This Idempotency-Key was already used with a different request",
                    idempotency_key=key,
                )
            return IdempotentResult(existing["response_status"], existing["response_body"], replayed=True)

        status_code, body = operation()

        conn.execute(
            """
            UPDATE idempotency_records
            SET response_status = %s, response_body = %s
            WHERE user_id = %s AND idempotency_key = %s
            """,
            (status_code, Jsonb(body), user_id, key),
        )
        return IdempotentResult(status_code, body, replayed=False)


def cleanup_expired(conn: DBConn, limit: int = 1000) -> int:
    """Purge expired idempotency records in batches using SKIP LOCKED."""
    deleted = conn.execute(
        """
        WITH to_delete AS (
            SELECT user_id, idempotency_key
            FROM idempotency_records
            WHERE expires_at <= now()
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        DELETE FROM idempotency_records i
        USING to_delete d
        WHERE i.user_id = d.user_id AND i.idempotency_key = d.idempotency_key
        RETURNING 1
        """,
        (limit,),
    ).fetchall()
    return len(deleted)
