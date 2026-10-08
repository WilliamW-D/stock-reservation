from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def create_pool(database_url: str, *, max_size: int = 20) -> ConnectionPool:
    """Connections run in autocommit mode.

    Nothing is ever left in an implicit transaction: every multi-statement
    operation opens an explicit ``with conn.transaction():`` block, so the
    transaction boundary is visible in the code that needs it.
    """
    return ConnectionPool(
        database_url,
        min_size=1,
        max_size=max_size,
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=True,
    )


def connect(database_url: str, **kwargs) -> psycopg.Connection:
    return psycopg.connect(database_url, autocommit=True, row_factory=dict_row, **kwargs)


def apply_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
