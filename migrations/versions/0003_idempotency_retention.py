"""0003_idempotency_retention

Revision ID: 0003_idempotency_retention
Revises: 0002_multi_product
Create Date: 2026-10-08 03:12:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_idempotency_retention"
down_revision: str | None = "0002_multi_product"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_SQL = """
ALTER TABLE idempotency_records
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '24 hours');

CREATE INDEX IF NOT EXISTS idempotency_records_expires_at_idx
    ON idempotency_records (expires_at);
"""

DOWNGRADE_SQL = """
DROP INDEX IF EXISTS idempotency_records_expires_at_idx;
ALTER TABLE idempotency_records DROP COLUMN IF EXISTS expires_at;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
