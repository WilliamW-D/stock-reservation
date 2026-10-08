"""Developer commands.

uv run python -m app.cli init-db   # create tables (idempotent)
uv run python -m app.cli seed      # 5 cases of cheese, 1 manager, 2 employees
"""

from __future__ import annotations

import sys

from app.config import Settings
from app.db import connect, run_migrations
from app.services import inventory, users

DEMO_USERS = [
    ("maria", "manager-pass", "manager"),
    ("eli", "employee-pass", "employee"),
    ("erin", "employee-pass", "employee"),
]
CHEESE_SKU = "CHEESE-CHEDDAR-CASE"


def init_db(url: str) -> None:
    run_migrations(url)
    print("schema and migrations applied")


def seed(url: str) -> None:
    run_migrations(url)
    with connect(url) as conn:
        for username, password, role in DEMO_USERS:
            if users.get_user_by_username(conn, username) is None:
                users.create_user(conn, username, password, role)
                print(f"created {role} {username!r} (password: {password})")
        manager = users.get_user_by_username(conn, "maria")
        assert manager is not None, "Manager 'maria' must exist after seeding users"
        exists = conn.execute("SELECT id FROM products WHERE sku = %s", (CHEESE_SKU,)).fetchone()
        if exists is None:
            product = inventory.create_product(conn, manager, sku=CHEESE_SKU, name="Cheddar cheese", unit="case")
            inventory.receive(conn, manager, product["product_id"], 5, "Opening stock")
            print("created product Cheddar cheese with 5 cases on hand")
        else:
            print("product already seeded")


def main(argv: list[str]) -> int:
    url = Settings.from_env().database_url
    commands = {"init-db": init_db, "seed": seed}
    if len(argv) != 1 or argv[0] not in commands:
        print(__doc__)
        return 2
    commands[argv[0]](url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
