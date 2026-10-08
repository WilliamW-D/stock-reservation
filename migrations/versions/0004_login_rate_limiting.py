"""0004_login_rate_limiting

Revision ID: 0004_login_rate_limiting
Revises: 0003_idempotency_retention
Create Date: 2026-10-08 03:15:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_login_rate_limiting"
down_revision: str | None = "0003_idempotency_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_SQL = """
CREATE TABLE IF NOT EXISTS login_attempts (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ip_address   TEXT        NOT NULL,
    username     TEXT        NOT NULL,
    success      BOOLEAN     NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS login_attempts_window_idx
    ON login_attempts (ip_address, username, attempted_at DESC);
"""

DOWNGRADE_SQL = """
DROP TABLE IF EXISTS login_attempts CASCADE;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
