from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = "postgresql://stock:stock@localhost:5433/stock"
    jwt_secret: str = "dev-only-secret-change-me-0123456789abcdef0123456789"
    jwt_ttl_minutes: int = 120
    # 0 disables the background expiry sweeper (tests trigger expiry explicitly).
    expiry_sweep_seconds: int = 15
    pool_max_size: int = 20

    @classmethod
    def from_env(cls) -> Settings:
        defaults = cls()
        return cls(
            database_url=os.getenv("DATABASE_URL", defaults.database_url),
            jwt_secret=os.getenv("JWT_SECRET", defaults.jwt_secret),
            jwt_ttl_minutes=int(os.getenv("JWT_TTL_MINUTES", defaults.jwt_ttl_minutes)),
            expiry_sweep_seconds=int(os.getenv("EXPIRY_SWEEP_SECONDS", defaults.expiry_sweep_seconds)),
            pool_max_size=int(os.getenv("DB_POOL_MAX_SIZE", defaults.pool_max_size)),
        )
