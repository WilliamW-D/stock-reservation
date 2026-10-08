"""0002_multi_product_orders

Revision ID: 0002_multi_product
Revises: 0001_initial
Create Date: 2026-10-08 02:40:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_multi_product"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_SQL = """
-- 1. Create reservation_lines
CREATE TABLE IF NOT EXISTS reservation_lines (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    reservation_id     BIGINT      NOT NULL REFERENCES reservations (id) ON DELETE CASCADE,
    product_id         BIGINT      NOT NULL REFERENCES products (id),
    quantity           INTEGER     NOT NULL CHECK (quantity > 0),
    fulfilled_quantity INTEGER     NOT NULL DEFAULT 0 CHECK (fulfilled_quantity >= 0),
    status             TEXT        NOT NULL DEFAULT 'active'
                                   CHECK (status IN ('active', 'partially_fulfilled', 'fulfilled', 'cancelled', 'expired')),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at          TIMESTAMPTZ,
    CONSTRAINT lines_fulfilled_within_quantity CHECK (fulfilled_quantity <= quantity),
    CONSTRAINT lines_closed_matches_status CHECK (
        (status IN ('active', 'partially_fulfilled')) = (closed_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS reservation_lines_reservation_idx
    ON reservation_lines (reservation_id);
CREATE INDEX IF NOT EXISTS reservation_lines_product_active_idx
    ON reservation_lines (product_id) WHERE status IN ('active', 'partially_fulfilled');

-- 2. Migrate existing single-product reservation data to reservation_lines
INSERT INTO reservation_lines (
    reservation_id, product_id, quantity, fulfilled_quantity, status, created_at, closed_at
)
SELECT id, product_id, quantity,
       CASE WHEN status = 'fulfilled' THEN quantity ELSE 0 END,
       status, created_at, closed_at
FROM reservations;

-- 3. Relax reservations product_id and quantity to allow multi-product orders
ALTER TABLE reservations ALTER COLUMN product_id DROP NOT NULL;
ALTER TABLE reservations ALTER COLUMN quantity DROP NOT NULL;

-- 4. Update reservations status check to support 'partially_fulfilled'
ALTER TABLE reservations DROP CONSTRAINT IF EXISTS reservations_status_check;
ALTER TABLE reservations ADD CONSTRAINT reservations_status_check
    CHECK (status IN ('active', 'partially_fulfilled', 'fulfilled', 'cancelled', 'expired'));

-- 5. Update reservations closed_at constraint to account for partially_fulfilled
ALTER TABLE reservations DROP CONSTRAINT IF EXISTS reservations_closed_at_matches_status;
ALTER TABLE reservations ADD CONSTRAINT reservations_closed_at_matches_status
    CHECK ((status IN ('active', 'partially_fulfilled')) = (closed_at IS NULL));

-- 6. Add reservation_line_id to audit_events
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS reservation_line_id BIGINT REFERENCES reservation_lines (id);
"""

DOWNGRADE_SQL = """
ALTER TABLE audit_events DROP COLUMN IF EXISTS reservation_line_id;
DROP TABLE IF EXISTS reservation_lines CASCADE;
ALTER TABLE reservations ALTER COLUMN product_id SET NOT NULL;
ALTER TABLE reservations ALTER COLUMN quantity SET NOT NULL;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
