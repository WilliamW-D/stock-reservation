# Cellar — a stock reservation service that cannot oversell

A restaurant has **five cases of cheese** and **five cases of butter**. Employees reserve cases for upcoming
orders; managers receive deliveries, record spoilage, and inspect audit logs. The web interface is a demo —
the main value of this project is the **formal correctness guarantees** and the automated tests
that prove them against a real PostgreSQL 18 database under high concurrency, retries, and failure injections.

**Stack:** Python 3.12+ · FastAPI · PostgreSQL 18 · psycopg 3 (explicit raw SQL) · Alembic · Docker Compose · Nginx · pytest

---

## Correctness Invariants (enforced and tested)

1. **`0 <= reserved_quantity <= on_hand_quantity`**: physical and reserved stock cannot be negative, and reserved stock can never exceed on-hand inventory.
2. **`available_quantity = on_hand_quantity - reserved_quantity`**: PostgreSQL generated column; mathematically impossible to drift.
3. **Atomic transitions**: terminal states (`fulfilled`, `cancelled`, `expired`) happen at most once (`SELECT ... FOR UPDATE` + status validation).
4. **Idempotent retries**: `(user_id, idempotency_key)` primary key + SHA-256 payload fingerprinting + 24h retention window with `SKIP LOCKED` batch purge.
5. **Append-only audit trail**: immutable ledger enforced via PostgreSQL trigger; written in the exact same transaction as stock changes.
6. **All-or-nothing rollback**: injected failures or partial availability leave inventory 100% untouched.
7. **Deadlock-free multi-product reservations**: multiple products acquired in deterministic ascending `product_id` order under row locks.
8. **Stateless horizontal scale**: multi-instance safe across Nginx proxy, background sweepers with `FOR UPDATE SKIP LOCKED`, and PostgreSQL-backed login rate limiting.

[`INVARIANTS.md`](INVARIANTS.md) details each invariant's mathematical rule, database enforcement mechanism, and verifying test. After **every single test**, a whole-database invariant verification runs: stock is within bounds, `reserved` equals the exact active reservation sum, and audit deltas explain 100% of current inventory.

---

## High-Concurrency Load Test Results

Running the async concurrent load harness (`scripts/load_test.py`) against live instances:

| Metric | Measured Result | Guarantee Verified |
|---|---|---|
| **Operations** | 250 mixed requests (reserve, fulfill, cancel, receive, replay) | Concurrent multi-client traffic |
| **Concurrency** | 15 parallel workers | Real row contention |
| **Throughput** | **224.0 req/s** | Fast row-level lock release |
| **Latency p50** | **20.9 ms** | Sub-30ms median latency |
| **Latency p95** | **299.8 ms** | Predictable tail under queue contention |
| **Idempotent Replays** | 13 verified | Zero double-allocation on retries |
| **Conflict Rejections (409)** | 59 handled cleanly | Database constraints protect inventory |
| **Invariant Check** | **100% SATISFIED** | Zero overselling, zero audit discrepancies |

---

## Architecture & Deployment

```
                +----------------------------+
                |    Client / Browser / UI   |
                +--------------+-------------+
                               |
                               v :8000
                +----------------------------+
                |     Nginx Reverse Proxy    |
                +-------+------------+-------+
                        |            |  (Round Robin)
                        v            v
           +---------------+      +---------------+
           |  App Instance |      |  App Instance |
           |     app-1     |      |     app-2     |
           +-------+-------+      +-------+-------+
                   |                      |
                   +----------+-----------+
                              | Connection Pool (psycopg3)
                              v :5432
           +--------------------------------------+
           |          PostgreSQL 18               |
           |  - Generated columns (available)     |
           |  - Deterministic ordered row locks   |
           |  - FOR UPDATE SKIP LOCKED sweepers   |
           |  - Append-only audit trigger         |
           |  - PostgreSQL-backed rate limiter    |
           +--------------------------------------+
```

---

## Quickstart

### Option 1: Full Docker Compose Multi-Instance Setup
```bash
# Starts PostgreSQL 18, runs Alembic migrations, starts app-1 & app-2, and Nginx proxy on :8000
docker compose up -d --build

# Run load test against the multi-instance proxy
uv run python scripts/load_test.py --url http://localhost:8000 --total 250 --concurrency 15
```

### Option 2: Local Development with `uv`
```bash
# 1. Start database
docker compose up -d db --wait

# 2. Install dependencies & run raw-SQL Alembic migrations
uv sync
uv run alembic upgrade head

# 3. Seed demo products & accounts
uv run python -m app.cli seed

# 4. Run application
uv run uvicorn app.main:app --port 8000

# 5. Run complete test suite (75 tests)
uv run pytest
```

Demo accounts:
- Manager: `maria` / `manager-pass`
- Employees: `eli` / `employee-pass`, `erin` / `employee-pass`

---

## Technical Deep-Dives: Race → Mechanism → Proof

### 1. The Last Case of Cheese (Single & Multi-Product)
- **Race:** Eli and Erin simultaneously reserve the final case of cheese.
- **Mechanism:** Conditional update with deterministic ordering:
  ```sql
  SELECT * FROM inventory WHERE product_id = ANY(:ids) ORDER BY product_id FOR UPDATE;
  UPDATE inventory
  SET reserved_quantity = reserved_quantity + :quantity
  WHERE product_id = :product_id AND on_hand_quantity - reserved_quantity >= :quantity;
  ```
- **Proof:** `tests/test_overselling.py` races threads through a barrier across 25 rounds; `tests/test_multiproduct.py` tests opposing orderings `{cheese, butter}` vs `{butter, cheese}` and proves zero deadlocks (`40P01`) occur.

### 2. Network Retries & Idempotency Retention
- **Race:** Network drops response; client retries identical reservation.
- **Mechanism:** Atomic `INSERT INTO idempotency_records (...) ON CONFLICT (user_id, idempotency_key) DO NOTHING` in the same transaction as stock reservation. 24-hour retention window with background `SKIP LOCKED` batch cleanup.
- **Proof:** `tests/test_idempotency.py` validates duplicate calls return identical results with `Idempotent-Replayed: true`. Expired keys are safely accepted as fresh requests.

### 3. Competing Transitions & Background Sweeper
- **Race:** Order cancelled while manager fulfills it, or background sweeper expires it during fulfillment.
- **Mechanism:** `SELECT * FROM reservations WHERE id = :id FOR UPDATE`, verify active status. Background sweepers use `SELECT id FROM reservations WHERE ... FOR UPDATE SKIP LOCKED LIMIT 1`, allowing multiple app instances to sweep expired orders concurrently without collisions or blocking.
- **Proof:** `tests/test_transitions.py` and `tests/test_multi_instance.py` prove terminal states occur strictly once.

### 4. Crash Mid-Operation (All-or-Nothing Rollback)
- **Mechanism:** Inventory changes, reservation lines, audit records, and idempotency states are committed in one atomic transaction.
- **Proof:** `tests/test_rollback.py` injects simulated crashes during audit logging; verifies stock remains untouched, no orphan rows exist.

### 5. Multi-Instance Statelessness & Rate Limiting
- **Mechanism:** No in-memory state; login rate limiting tracked in `login_attempts` table in PostgreSQL.
- **Proof:** `tests/test_multi_instance.py` and `tests/test_observability_and_rate_limit.py` verify cross-instance replay and that 5 failed logins return HTTP 429 across all nodes.

---

## Observability & Endpoints

- `GET /healthz`: Process health check and environment status (`{"status": "ok"}`).
- `GET /readyz`: Database connectivity probe (`{"status": "ready"}`).
- `GET /metrics`: Inventory totals, reservation status counts, and audit volume.
- `GET /audit-events`: Append-only audit history with stock deltas (Manager only).
- `X-Request-ID`: Distributed tracing header injected on all HTTP requests.

---

## Code Quality & CI

```bash
uv run ruff check .          # Linting (E, F, I, UP, B)
uv run ruff format --check . # Code formatting
uv run mypy app              # Strict type checking
uv run pytest                # 75 tests with invariant checks after every test
```
All checks run in `.github/workflows/ci.yml` against PostgreSQL 18 with repeated stress passes.
