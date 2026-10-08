from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

type DBConn = psycopg.Connection[dict[str, Any]]


def create_pool(database_url: str, *, max_size: int = 20) -> ConnectionPool[DBConn]:
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


def connect(database_url: str, **kwargs: Any) -> DBConn:
    return psycopg.connect(database_url, autocommit=True, row_factory=dict_row, **kwargs)


def apply_schema(conn: DBConn) -> None:
    conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))


def run_migrations(database_url: str | None = None) -> None:
    from alembic import command
    from alembic.config import Config

    ini_path = Path(__file__).resolve().parent.parent / "alembic.ini"
    alembic_cfg = Config(str(ini_path))
    if database_url:
        target_url = database_url
        if target_url.startswith("postgresql://"):
            target_url = target_url.replace("postgresql://", "postgresql+psycopg://", 1)
        alembic_cfg.set_main_option("sqlalchemy.url", target_url)
    command.upgrade(alembic_cfg, "head")
