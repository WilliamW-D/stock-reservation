"""Test fixtures. Tests run against a real PostgreSQL (see docker-compose.yml).

A fresh ``stock_test`` database is created per session, every table is truncated
before each test, and the full invariant sweep runs after each test.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from app.db import DBConn, connect, run_migrations
from app.services import inventory, users

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "postgresql://stock:stock@localhost:5433/stock_test")
FAST_HASH_ITERATIONS = 1_000  # tests do not need production-grade password hashing cost


def _admin_url(url: str) -> str:
    return make_conninfo(url, dbname="postgres")


@pytest.fixture(scope="session")
def database_url() -> str:
    dbname = conninfo_to_dict(TEST_DATABASE_URL)["dbname"]
    try:
        admin = psycopg.connect(_admin_url(TEST_DATABASE_URL), autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.exit(f"PostgreSQL is not reachable ({exc}). Run `docker compose up -d` first.", returncode=2)
    with admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname)))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    run_migrations(TEST_DATABASE_URL)
    return TEST_DATABASE_URL


@pytest.fixture
def new_conn(database_url):
    """Factory for *independent* database connections (one per simulated client)."""
    opened: list[psycopg.Connection] = []

    def factory() -> psycopg.Connection:
        # lock_timeout keeps a buggy test from hanging forever on a lock.
        c = connect(database_url, options="-c lock_timeout=10s")
        opened.append(c)
        return c

    yield factory
    for c in opened:
        c.close()


@pytest.fixture
def conn(new_conn) -> psycopg.Connection:
    return new_conn()


@pytest.fixture(autouse=True)
def clean_database(database_url):
    with connect(database_url) as c:
        c.execute(
            "TRUNCATE login_attempts, audit_events, idempotency_records, reservation_lines, reservations, inventory, products, users "
            "RESTART IDENTITY CASCADE"
        )
    yield
    with connect(database_url) as c:
        assert_invariants(c)


@pytest.fixture
def world(conn):
    """The restaurant: one manager, two employees, five cases of cheese (audited)."""
    manager = users.create_user(conn, "maria", "manager-pass", "manager", iterations=FAST_HASH_ITERATIONS)
    eli = users.create_user(conn, "eli", "employee-pass", "employee", iterations=FAST_HASH_ITERATIONS)
    erin = users.create_user(conn, "erin", "employee-pass", "employee", iterations=FAST_HASH_ITERATIONS)
    cheese = inventory.create_product(conn, manager, sku="CHEESE-CHEDDAR-CASE", name="Cheddar cheese", unit="case")
    inventory.receive(conn, manager, cheese["product_id"], 5, "Opening stock")
    return SimpleNamespace(manager=manager, eli=eli, erin=erin, cheese=cheese["product_id"])


def make_product(conn, manager, sku: str, on_hand: int) -> int:
    product = inventory.create_product(conn, manager, sku=sku, name=sku, unit="case")
    if on_hand:
        inventory.receive(conn, manager, product["product_id"], on_hand, "Test stock")
    return product["product_id"]


def stock(conn, product_id: int) -> tuple[int, int]:
    row = conn.execute(
        "SELECT on_hand_quantity, reserved_quantity FROM inventory WHERE product_id = %s", (product_id,)
    ).fetchone()
    return row["on_hand_quantity"], row["reserved_quantity"]


def count(conn, query: str, params=()) -> int:
    return conn.execute(f"SELECT count(*) AS n FROM ({query}) q", params).fetchone()["n"]


def assert_invariants(conn: DBConn) -> None:
    """The whole-database check that runs after every test (see INVARIANTS.md)."""
    bad_inventory = conn.execute(
        """
        SELECT i.product_id, i.on_hand_quantity, i.reserved_quantity, i.available_quantity,
               COALESCE((SELECT sum(quantity - fulfilled_quantity) FROM reservation_lines rl
                         WHERE rl.product_id = i.product_id AND rl.status IN ('active', 'partially_fulfilled')), 0) AS active_sum,
               COALESCE((SELECT sum(on_hand_delta) FROM audit_events a WHERE a.product_id = i.product_id), 0)
                   AS audited_on_hand,
               COALESCE((SELECT sum(reserved_delta) FROM audit_events a WHERE a.product_id = i.product_id), 0)
                   AS audited_reserved
        FROM inventory i
        """
    ).fetchall()
    for row in bad_inventory:
        pid = row["product_id"]
        assert 0 <= row["reserved_quantity"] <= row["on_hand_quantity"], f"I1/I2 violated for product {pid}: {row}"
        assert row["available_quantity"] == row["on_hand_quantity"] - row["reserved_quantity"], f"I3: {row}"
        assert row["reserved_quantity"] == row["active_sum"], f"reserved != active reservations: {row}"
        assert row["on_hand_quantity"] == row["audited_on_hand"], f"I6 on_hand not explained by audit: {row}"
        assert row["reserved_quantity"] == row["audited_reserved"], f"I6 reserved not explained by audit: {row}"

    line_counts = conn.execute(
        """
        SELECT rl.id, rl.status,
               count(*) FILTER (WHERE a.action = 'reserve') AS reserves,
               count(*) FILTER (WHERE a.action IN ('cancel', 'fulfill', 'expire')) AS terminals
        FROM reservation_lines rl
        LEFT JOIN audit_events a
          ON (a.reservation_line_id = rl.id OR (a.reservation_line_id IS NULL AND a.reservation_id = rl.reservation_id))
        GROUP BY rl.id, rl.status
        """
    ).fetchall()
    for row in line_counts:
        assert row["reserves"] == 1, f"reservation line {row['id']} has {row['reserves']} reserve events"
        if row["status"] == "active":
            assert row["terminals"] == 0, f"active line {row['id']} has terminal event: {row}"
        elif row["status"] in ("cancelled", "expired", "fulfilled"):
            assert row["terminals"] >= 1, f"terminal line {row['id']} has no terminal event: {row}"
