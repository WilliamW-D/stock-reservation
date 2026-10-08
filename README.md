# Cellar — a stock reservation service that cannot oversell

A restaurant has **five cases of cheese**. Employees reserve cases for upcoming
orders; managers receive deliveries and record spoilage. The interface is a demo —
the point of this project is the **correctness guarantees** and the automated tests
that prove them against a real PostgreSQL database.

**Stack:** Python · FastAPI · PostgreSQL 18 · psycopg 3 (explicit SQL) · pytest

## The rules (written before the code)

1. Stock and reserved quantities cannot be negative.
2. Reserved stock cannot exceed physical stock.
3. Available = physical − reserved.
4. A reservation is fulfilled, cancelled, or expired only once.
5. Retrying the same request returns the original result.
6. Every successful stock change has an audit record.
7. Failed operations leave stock unchanged.

[`INVARIANTS.md`](INVARIANTS.md) maps each rule to the mechanism that enforces it
and the test that proves it. After **every** test, an invariant check verifies the
whole database: stock is in range, `reserved` equals the sum of active reservations,
and the audit log's deltas add up exactly to current stock.

## Run it

```bash
docker compose up -d --wait          # PostgreSQL 18 on localhost:5433
uv sync                              # dependencies
uv run python -m app.cli seed        # schema + 5 cases of cheese + users
uv run uvicorn app.main:app          # http://localhost:8000  (API docs: /docs)
uv run pytest                        # 57 tests against a real database
```

Demo users: `maria` / `manager-pass` (manager), `eli` and `erin` / `employee-pass` (employees).

## Interview walkthrough: race → mechanism → proof

### 1. The last case of cheese

**Race.** One case left. Eli and Erin both reserve it at the same moment. The
naive approach — `SELECT available`, check in Python, then `UPDATE` — lets both
read `1`, both pass the check, and both write.

**Mechanism.** The check *is* the update ([reservations.py](app/services/reservations.py)):

```sql
UPDATE inventory
SET reserved_quantity = reserved_quantity + :quantity
WHERE product_id = :product_id
  AND on_hand_quantity - reserved_quantity >= :quantity
RETURNING product_id, on_hand_quantity, reserved_quantity, available_quantity;
```

The first transaction row-locks the inventory row. The second blocks; when the
first commits, PostgreSQL **re-evaluates the `WHERE` against the committed row**,
matches nothing, and returns no row → `409 insufficient_stock` (or `404` if the
product doesn't exist). `CHECK (reserved_quantity <= on_hand_quantity)` is the
second line of defence.

**Proof** ([test_overselling.py](tests/test_overselling.py)):
- `test_two_connections_race_for_the_last_unit` — two independent connections,
  released together by a `threading.Barrier`, 25 rounds: exactly one winner every time.
- `test_second_transaction_waits_for_the_first_then_rechecks` — *deterministic*:
  A reserves and holds its transaction open; the test polls `pg_stat_activity`
  until PostgreSQL reports B is **waiting on a lock**, then commits A. B fails.
  This proves the requests overlapped rather than hoping they did.
- `test_naive_read_then_write_is_stale_and_the_check_constraint_catches_it` —
  shows the naive pattern is broken and that the CHECK constraint stops it.

### 2. The double-click

**Race.** A flaky network makes the client send "reserve 2 cases" twice — possibly
simultaneously.

**Mechanism** ([idempotency.py](app/services/idempotency.py)). `Idempotency-Key` is
required. In **one transaction**: claim the key with
`INSERT … ON CONFLICT DO NOTHING` on `PRIMARY KEY (user_id, idempotency_key)`, do
the work, store the response. A concurrent duplicate blocks on the unique index
until the first commits, then reads and replays the stored result. Same key with a
different body → `409 idempotency_key_reused`. Because it's a database
constraint, it survives restarts and multiple app instances, which an in-memory
dictionary would not.

**Proof** ([test_idempotency.py](tests/test_idempotency.py)): 10 simultaneous
same-key requests → 1 reservation, 1 audit event, 1 stock change, 10 identical
responses; plus a deterministic version that observes the duplicate blocked on the key.

### 3. Cancel vs. fulfill

**Race.** Eli cancels his order at the same instant Maria fulfills it. If both
succeed, stock is decremented twice.

**Mechanism.** `SELECT … FOR UPDATE` on the reservation, then verify
`status = 'active'`. The loser waits on the row lock, then sees `cancelled` or
`fulfilled` → `409 invalid_transition`.

**Proof** ([test_transitions.py](tests/test_transitions.py)): 20 barrier-synced
rounds, exactly one terminal transition each; a deterministic lock-wait test;
and a 3×3 matrix showing every terminal state is final.

### 4. Crash mid-operation

**Mechanism.** The inventory update, reservation, audit event and idempotency
result share a single transaction.

**Proof** ([test_rollback.py](tests/test_rollback.py)): the audit write is replaced
with a crash. The fake first records the inventory *inside* the transaction
(proving the `UPDATE` ran), then raises. Afterwards: stock unchanged, no
reservation, no audit row, no idempotency record.

### 5. Validation and permissions

**Proof** ([test_api.py](tests/test_api.py)), end to end over HTTP: zero, negative,
fractional, string and boolean quantities → `422`; employees can't receive
or adjust stock, read the audit log, or force expiry → `403`; another employee's
reservation → `404` (existence isn't leaked); managers can act on anyone's.

## Stock effects

| Operation | Physical | Reserved |
|---|---|---|
| Reserve 2 | unchanged | +2 |
| Cancel / expire | unchanged | −2 |
| Fulfill | −2 | −2 |
| Receive delivery | +q | unchanged |
| Spoilage (adjust −q) | −q, only if result ≥ reserved | unchanged |

## Design decisions worth defending

- **psycopg 3 with explicit SQL, no ORM.** The guarantees live in a handful of SQL
  statements, and they should be readable in review.
- **Autocommit connections + explicit `with conn.transaction()`.** Every
  transaction boundary is visible in the code that needs it; nested blocks become
  savepoints, which is how the idempotency wrapper composes with the reservation.
- **Failed attempts are not cached as idempotent results.** A rejected request rolls
  back its key claim, so retrying after a delivery can succeed.
- **`available_quantity` is a generated column**, so it can't drift from the
  numbers it's derived from.
- **The audit log is append-only**, enforced by a trigger.
- **Expiry** runs in a background sweeper (each reservation in its own transaction)
  and on demand via `POST /reservations/expire`. Fulfilling a reservation past its
  expiry time is rejected even if the sweeper hasn't run yet.

## Next steps (deliberately out of scope for v1)

- Multi-product orders (all-or-nothing reservation across rows; lock in a
  consistent product order to avoid deadlocks).
- Multiple locations; idempotency-record retention; migrations tooling (Alembic).
