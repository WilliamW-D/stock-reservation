# Invariants

These rules were defined before writing code. Every invariant is enforced by an explicit
database mechanism and proven by an automated test suite against a real PostgreSQL instance.

| #  | Rule | Enforced by | Proven by |
|----|------|-------------|-----------|
| **I1** | Physical (`on_hand`) and reserved quantities are never negative. | `CHECK (on_hand_quantity >= 0)`, `CHECK (reserved_quantity >= 0)` | `test_rollback.py::test_check_constraints_are_a_second_line_of_defence` |
| **I2** | Reserved stock never exceeds physical stock. | Conditional `UPDATE … WHERE on_hand - reserved >= :qty` (primary), `CHECK (reserved_quantity <= on_hand_quantity)` (backstop) | `test_overselling.py` (all tests) |
| **I3** | Available = physical − reserved. | `available_quantity` is a `GENERATED ALWAYS … STORED` column; mathematically impossible to drift. | `test_api.py::test_inventory_reports_available_as_on_hand_minus_reserved` |
| **I4** | Terminal transitions (`fulfilled`, `cancelled`, `expired`) happen at most once. | `SELECT … FOR UPDATE` on parent and lines + status validation; `CHECK ((status IN ('active', 'partially_fulfilled')) = (closed_at IS NULL))`. | `test_transitions.py` |
| **I5** | Retrying the same request returns the original result without re-allocating stock. | `PRIMARY KEY (user_id, idempotency_key)`; key claimed in the same transaction as the work; SHA-256 fingerprint validation on replay; 24h retention window. | `test_idempotency.py` |
| **I6** | Every successful stock change has an audit record. | Audit row inserted inside the exact same transaction as the stock change; `audit_events` is append-only (PostgreSQL trigger). | Invariant sweep after **every** test (`conftest.py::assert_invariants`) |
| **I7** | Failed operations leave stock completely unchanged. | Atomic transaction rollback; any error rolls back inventory, reservation lines, audit and idempotency claims together. | `test_rollback.py` |
| **I8** | Multi-product orders are deadlock-free across concurrent clients. | Row locks are acquired in deterministic ascending `product_id` order (`FOR UPDATE`) before executing line updates. | `test_multiproduct.py::test_concurrent_opposing_orders_deadlock_free` |
| **I9** | Multi-instance workers never duplicate background expiry or cleanup. | Batch claims use `SELECT … FOR UPDATE SKIP LOCKED LIMIT 1`, cleanly partitioning work across app processes with zero lock contention. | `test_multi_instance.py::test_concurrent_sweepers_across_instances` |

---

## The Whole-Database Invariant Sweep

After **every single test** in the suite, and immediately following the 250-operation high-concurrency load test, `tests/conftest.py` executes an exhaustive verification over the entire database:

1. `0 <= reserved_quantity <= on_hand_quantity` for every product.
2. `available_quantity == on_hand_quantity - reserved_quantity` for every inventory row.
3. `reserved_quantity` equals the exact sum of unfulfilled quantities across active and partially fulfilled reservation lines.
4. The sum of audit `on_hand_delta` equals `on_hand_quantity`, and the sum of `reserved_delta` equals `reserved_quantity` (the immutable audit ledger explains 100% of current inventory).
5. Every reservation line has exactly one `reserve` event, active lines have 0 terminal events, and closed lines have at least one terminal event (`cancel`, `fulfill`, `expire`).

---

## Deliberate Design Decisions

- **Failed attempts are not cached as idempotent results.** A rejected request (e.g. insufficient stock) rolls back *everything*, including the key claim, so retrying after a delivery can succeed. Only successful operations are replayed.
- **Idempotency keys are scoped per user.** Two employees using the same key string cannot collide.
- **Expired keys are treated as fresh requests.** After 24 hours, re-using an idempotency key is accepted as a new request rather than rejected, matching standard API retention semantics.
- **Deterministic lock ordering prevents deadlocks.** Ordering products ascending before acquiring `FOR UPDATE` locks completely eliminates PostgreSQL `40P01` deadlocks in multi-product transactions.
- **Spoilage cannot eat into reserved stock.** An adjustment that would leave `on_hand < reserved` is rejected with `409`; reservations are never silently cancelled.
- **Another employee's reservation returns `404`, not `403`**, preventing information leakage.
- **Fulfilling a reservation past its expiry time is rejected** even if the sweeper has not executed yet.
- **Rate limiting is stateful in PostgreSQL**, ensuring brute-force protection works across multiple container instances without relying on in-memory counters.
