from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = "postgresql://stock:stock@localhost:5433/stock"
    jwt_secret: str = "dev-only-secret-change-me-0123456789abcdef0123456789"
    jwt_ttl_minutes: int = 120
    environment: str = "development"
    # 0 disables the background expiry sweeper (tests trigger expiry explicitly).
    expiry_sweep_seconds: int = 15
    pool_max_size: int = 20
    lock_timeout_ms: int = 2500
    statement_timeout_ms: int = 5000
    idle_in_transaction_timeout_ms: int = 10000

    @classmethod
    def from_env(cls) -> Settings:
        defaults = cls()
        env = os.getenv("ENVIRONMENT", defaults.environment)
        secret = os.getenv("JWT_SECRET", defaults.jwt_secret)
        if env == "production" and (not secret or secret == defaults.jwt_secret):
            raise ValueError("JWT_SECRET must be explicitly set and cannot use default dev secret in production")
        return cls(
            database_url=os.getenv("DATABASE_URL", defaults.database_url),
            jwt_secret=secret,
            jwt_ttl_minutes=int(os.getenv("JWT_TTL_MINUTES", defaults.jwt_ttl_minutes)),
            environment=env,
            expiry_sweep_seconds=int(os.getenv("EXPIRY_SWEEP_SECONDS", defaults.expiry_sweep_seconds)),
            pool_max_size=int(os.getenv("DB_POOL_MAX_SIZE", defaults.pool_max_size)),
            lock_timeout_ms=int(os.getenv("DB_LOCK_TIMEOUT_MS", defaults.lock_timeout_ms)),
            statement_timeout_ms=int(os.getenv("DB_STATEMENT_TIMEOUT_MS", defaults.statement_timeout_ms)),
            idle_in_transaction_timeout_ms=int(
                os.getenv("DB_IDLE_TIMEOUT_MS", defaults.idle_in_transaction_timeout_ms)
            ),
        )
