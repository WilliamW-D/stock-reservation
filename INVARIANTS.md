# Invariants

These rules were written before the code. Every one is enforced by a specific
mechanism and proven by a specific automated test against a real PostgreSQL.

| #  | Rule | Enforced by | Proven by |
|----|------|-------------|-----------|
| I1 | Physical (`on_hand`) and reserved quantities are never negative. | `CHECK (on_hand_quantity >= 0)`, `CHECK (reserved_quantity >= 0)` | `test_rollback.py::test_check_constraints_are_a_second_line_of_defence` |
| I2 | Reserved stock never exceeds physical stock. | Conditional `UPDATE … WHERE on_hand - reserved >= :qty` (primary), `CHECK (reserved_quantity <= on_hand_quantity)` (backstop) | `test_overselling.py` (all tests) |
| I3 | Available = physical − reserved. | `available_quantity` is a `GENERATED ALWAYS … STORED` column; it cannot drift. | `test_api.py::test_inventory_reports_available_as_on_hand_minus_reserved` |
| I4 | A reservation is fulfilled, cancelled, or expired at most once. | `SELECT … FOR UPDATE` on the reservation + status check; status-guarded `UPDATE` as backstop; `CHECK` tying `closed_at` to status. | `test_transitions.py` |
| I5 | Retrying the same request returns the original result. | `PRIMARY KEY (user_id, idempotency_key)`; key claimed in the same transaction as the work; request fingerprint compared on replay. | `test_idempotency.py` |
| I6 | Every successful stock change has an audit record. | Audit insert is inside the same transaction as the stock change; `audit_events` is append-only (trigger). | Invariant sweep after **every** test (`conftest.py::assert_invariants`) |
| I7 | Failed operations leave stock unchanged. | One transaction per operation; any exception rolls back inventory, reservation, audit and idempotency rows together. | `test_rollback.py` |

## The invariant sweep

After every test, `tests/conftest.py` checks the whole database:

- `0 <= reserved_quantity <= on_hand_quantity` for every product.
- `reserved_quantity` equals the sum of `active` reservation quantities.
- The sum of audit `on_hand_delta` equals `on_hand_quantity`, and the sum of
  `reserved_delta` equals `reserved_quantity` (the audit log fully explains stock).
- Every reservation has exactly one `reserve` event, and every closed reservation
  has exactly one terminal event (`cancel` / `fulfill` / `expire`).

## Deliberate decisions

- **Failed attempts are not stored as idempotent results.** A rejected request
  (e.g. insufficient stock) rolls back *everything*, including the key claim, so a
  retry after a delivery can succeed. Only successful results are replayed.
- **Idempotency keys are scoped per user.** Two employees cannot collide.
- **Spoilage cannot eat into reserved stock.** An adjustment that would leave
  `on_hand < reserved` is rejected with `409`; reservations are never silently cancelled.
- **Another employee's reservation returns `404`, not `403`**, so existence is not leaked.
- **Fulfilling a reservation past its expiry time is rejected** even if the sweeper
  has not run yet; cancelling it is allowed (same stock effect as expiry).
