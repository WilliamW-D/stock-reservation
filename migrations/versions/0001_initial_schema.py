"""0001_initial_schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-08 02:30:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username      TEXT        NOT NULL UNIQUE,
    password_hash TEXT        NOT NULL,
    role          TEXT        NOT NULL CHECK (role IN ('employee', 'manager')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS products (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku        TEXT        NOT NULL UNIQUE,
    name       TEXT        NOT NULL,
    unit       TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS inventory (
    product_id         BIGINT      PRIMARY KEY REFERENCES products (id),
    on_hand_quantity   INTEGER     NOT NULL DEFAULT 0,
    reserved_quantity  INTEGER     NOT NULL DEFAULT 0,
    available_quantity INTEGER     GENERATED ALWAYS AS (on_hand_quantity - reserved_quantity) STORED,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT inventory_on_hand_non_negative   CHECK (on_hand_quantity >= 0),
    CONSTRAINT inventory_reserved_non_negative  CHECK (reserved_quantity >= 0),
    CONSTRAINT inventory_reserved_within_on_hand CHECK (reserved_quantity <= on_hand_quantity)
);

CREATE TABLE IF NOT EXISTS reservations (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id      BIGINT      NOT NULL REFERENCES products (id),
    user_id         BIGINT      NOT NULL REFERENCES users (id),
    order_reference TEXT        NOT NULL CHECK (length(order_reference) BETWEEN 1 AND 100),
    quantity        INTEGER     NOT NULL CHECK (quantity > 0),
    status          TEXT        NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active', 'fulfilled', 'cancelled', 'expired')),
    expires_at      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at       TIMESTAMPTZ,
    CONSTRAINT reservations_closed_at_matches_status CHECK ((status = 'active') = (closed_at IS NULL))
);

CREATE INDEX IF NOT EXISTS reservations_active_expiry_idx
    ON reservations (expires_at) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS reservations_user_idx
    ON reservations (user_id, id DESC);

CREATE TABLE IF NOT EXISTS idempotency_records (
    user_id             BIGINT      NOT NULL REFERENCES users (id),
    idempotency_key     TEXT        NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 255),
    request_fingerprint TEXT        NOT NULL,
    response_status     INTEGER,
    response_body       JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_id        BIGINT      REFERENCES users (id),
    action          TEXT        NOT NULL
                                CHECK (action IN ('reserve', 'cancel', 'expire', 'fulfill', 'receive', 'adjust')),
    product_id      BIGINT      NOT NULL REFERENCES products (id),
    reservation_id  BIGINT      REFERENCES reservations (id),
    on_hand_delta   INTEGER     NOT NULL,
    reserved_delta  INTEGER     NOT NULL,
    on_hand_after   INTEGER     NOT NULL,
    reserved_after  INTEGER     NOT NULL,
    reason          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT audit_events_changes_stock CHECK (on_hand_delta <> 0 OR reserved_delta <> 0)
);

CREATE INDEX IF NOT EXISTS audit_events_product_idx ON audit_events (product_id, id DESC);

CREATE OR REPLACE FUNCTION forbid_audit_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only';
END
$$;

CREATE OR REPLACE TRIGGER audit_events_append_only
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION forbid_audit_mutation();
"""

DOWNGRADE_SQL = """
DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events;
DROP FUNCTION IF EXISTS forbid_audit_mutation;
DROP TABLE IF EXISTS audit_events CASCADE;
DROP TABLE IF EXISTS idempotency_records CASCADE;
DROP TABLE IF EXISTS reservations CASCADE;
DROP TABLE IF EXISTS inventory CASCADE;
DROP TABLE IF EXISTS products CASCADE;
DROP TABLE IF EXISTS users CASCADE;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
