"""Service layer. Every function takes an explicit psycopg connection.

Functions that change stock open their own ``with conn.transaction():`` block.
If the caller already holds a transaction, that block becomes a savepoint, which
lets callers (the idempotency wrapper, tests) compose operations atomically.
"""
