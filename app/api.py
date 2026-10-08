"""HTTP layer. Thin: parse/validate input, resolve the user, call a service, map errors."""

# NOTE: no `from __future__ import annotations` here. FastAPI resolves string
# annotations against module globals, and the dependency aliases (Conn,
# CurrentUser, ...) are defined inside create_app().
import logging
import threading
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal, Self

import psycopg
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import Settings
from app.db import DBConn, create_pool
from app.errors import AuthenticationError, DomainError
from app.security import create_access_token, decode_access_token
from app.services import audit, idempotency, inventory, reservations, users
from app.services.common import MAX_QUANTITY, User, require_manager

log = logging.getLogger("stock")
STATIC_DIR = Path(__file__).with_name("static")

Quantity = Annotated[int, Field(strict=True, gt=0, le=MAX_QUANTITY)]


# ---------------------------------------------------------------- schemas
class ReservationLineItem(BaseModel):
    product_id: Annotated[int, Field(strict=True, gt=0)]
    quantity: Quantity


class ReservationCreate(BaseModel):
    order_reference: Annotated[str, Field(min_length=1, max_length=100)]
    ttl_minutes: Annotated[int, Field(strict=True, ge=1, le=reservations.MAX_TTL_MINUTES)] = 30
    items: list[ReservationLineItem] | None = None
    product_id: Annotated[int, Field(strict=True, gt=0)] | None = None
    quantity: Quantity | None = None

    @model_validator(mode="after")
    def check_items_or_product(self) -> Self:
        if not self.items and (self.product_id is None or self.quantity is None):
            raise ValueError("Either items list or product_id and quantity must be provided")
        return self


class TransitionRequest(BaseModel):
    reason: Annotated[str | None, Field(max_length=500)] = None


class LineTransitionRequest(BaseModel):
    quantity: Quantity | None = None
    reason: Annotated[str | None, Field(max_length=500)] = None


class StockReceive(BaseModel):
    quantity: Quantity
    reason: Annotated[str | None, Field(max_length=500)] = None


class StockAdjust(BaseModel):
    delta: Annotated[int, Field(strict=True, ge=-MAX_QUANTITY, le=MAX_QUANTITY)]
    reason: Annotated[str, Field(min_length=1, max_length=500)]

    @field_validator("delta")
    @classmethod
    def non_zero(cls, v: int) -> int:
        if v == 0:
            raise ValueError("delta must be non-zero")
        return v


class ProductCreate(BaseModel):
    sku: Annotated[str, Field(min_length=1, max_length=64)]
    name: Annotated[str, Field(min_length=1, max_length=200)]
    unit: Annotated[str, Field(min_length=1, max_length=32)]


# ---------------------------------------------------------------- sweeper
class ExpirySweeper(threading.Thread):
    def __init__(self, pool, interval_seconds: int) -> None:
        super().__init__(name="expiry-sweeper", daemon=True)
        self.pool = pool
        self.interval = interval_seconds
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                with self.pool.connection() as conn:
                    n = reservations.expire_due(conn)
                    cleaned = idempotency.cleanup_expired(conn)
                if n:
                    log.info("expired %d reservation(s)", n)
                if cleaned:
                    log.info("cleaned %d expired idempotency record(s)", cleaned)
            except Exception:  # keep sweeping; one bad pass must not kill the thread
                log.exception("expiry sweep failed")


# ---------------------------------------------------------------- app
def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = create_pool(
            settings.database_url,
            max_size=settings.pool_max_size,
            lock_timeout_ms=settings.lock_timeout_ms,
            statement_timeout_ms=settings.statement_timeout_ms,
            idle_in_transaction_timeout_ms=settings.idle_in_transaction_timeout_ms,
        )
        app.state.pool = pool
        sweeper = None
        if settings.expiry_sweep_seconds > 0:
            sweeper = ExpirySweeper(pool, settings.expiry_sweep_seconds)
            sweeper.start()
        try:
            yield
        finally:
            if sweeper:
                sweeper.stop_event.set()
                sweeper.join(timeout=5)
            pool.close()

    app = FastAPI(
        title="Stock Reservation Service",
        description="Reserve restaurant stock with explicit correctness guarantees.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings

    @app.exception_handler(DomainError)
    async def domain_error_handler(_: Request, exc: DomainError):
        headers = {"WWW-Authenticate": "Bearer"} if isinstance(exc, AuthenticationError) else None
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
            headers=headers,
        )

    @app.exception_handler(psycopg.errors.CheckViolation)
    async def check_violation_handler(_: Request, exc: psycopg.errors.CheckViolation):
        # Second line of defence fired: the database refused an impossible state.
        log.error("CHECK constraint violation: %s", exc)
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "constraint_violation",
                    "message": "The database rejected a change that would violate a stock invariant",
                    "details": {"constraint": getattr(exc.diag, "constraint_name", None)},
                }
            },
        )

    @app.exception_handler(psycopg.errors.LockNotAvailable)
    @app.exception_handler(psycopg.errors.QueryCanceled)
    async def timeout_handler(_: Request, exc: psycopg.Error):
        log.warning("Database lock or statement timeout: %s", exc)
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "database_timeout",
                    "message": "Operation timed out waiting for database locks. Safe to retry.",
                    "details": {"sqlstate": getattr(exc.diag, "sqlstate", None)},
                }
            },
            headers={"Retry-After": "1"},
        )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        req_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        request.state.request_id = req_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = req_id
        return response

    # ------------------------------------------------------------ deps
    oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)

    def get_conn(request: Request) -> Iterator[DBConn]:
        with request.app.state.pool.connection() as conn:
            yield conn

    Conn = Annotated[DBConn, Depends(get_conn)]

    def current_user(conn: Conn, token: Annotated[str | None, Depends(oauth2)]) -> User:
        if not token:
            raise AuthenticationError("Missing bearer token")
        user = users.get_user(conn, decode_access_token(token, secret=settings.jwt_secret))
        if user is None:
            raise AuthenticationError("User no longer exists")
        return user

    CurrentUser = Annotated[User, Depends(current_user)]
    IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]

    def idempotent_response(result: idempotency.IdempotentResult) -> JSONResponse:
        return JSONResponse(
            status_code=result.status_code,
            content=result.body,
            headers={"Idempotent-Replayed": "true" if result.replayed else "false"},
        )

    # ------------------------------------------------------------ health & metrics
    @app.get("/healthz", tags=["system"])
    def healthz():
        return {"status": "ok", "environment": settings.environment}

    @app.get("/readyz", tags=["system"])
    def readyz(conn: Conn):
        conn.execute("SELECT 1")
        return {"status": "ready", "database": "connected"}

    @app.get("/metrics", tags=["system"])
    def get_metrics(conn: Conn):
        res_counts = conn.execute("SELECT status, count(*) AS count FROM reservations GROUP BY status").fetchall()
        inv = conn.execute(
            "SELECT count(*) AS total_products, COALESCE(sum(on_hand_quantity), 0) AS total_on_hand, COALESCE(sum(reserved_quantity), 0) AS total_reserved FROM inventory"
        ).fetchone()
        aud = conn.execute("SELECT count(*) AS total_audit_events FROM audit_events").fetchone()
        idem = conn.execute("SELECT count(*) AS total_idempotency_records FROM idempotency_records").fetchone()
        return {
            "inventory": inv,
            "reservations": {r["status"]: r["count"] for r in res_counts},
            "audit_events": aud["total_audit_events"] if aud else 0,
            "idempotency_records": idem["total_idempotency_records"] if idem else 0,
        }

    # ------------------------------------------------------------ auth
    @app.post("/auth/token", tags=["auth"])
    def login(request: Request, conn: Conn, form: Annotated[OAuth2PasswordRequestForm, Depends()]):
        client_ip = (
            request.headers.get("X-Forwarded-For", request.client.host if request.client else "127.0.0.1")
            .split(",")[0]
            .strip()
        )
        user = users.authenticate_with_rate_limit(conn, form.username, form.password, ip_address=client_ip)
        if user is None:
            raise AuthenticationError("Incorrect username or password")
        token = create_access_token(
            user.id, user.role, secret=settings.jwt_secret, ttl_minutes=settings.jwt_ttl_minutes
        )
        return {"access_token": token, "token_type": "bearer", "user": user.to_dict()}

    @app.get("/me", tags=["auth"])
    def me(user: CurrentUser):
        return user.to_dict()

    # ------------------------------------------------------------ products / inventory
    @app.get("/products", tags=["inventory"])
    def list_products(conn: Conn, _: CurrentUser):
        return inventory.list_inventory(conn)

    @app.post("/products", tags=["inventory"], status_code=201)
    def create_product(conn: Conn, user: CurrentUser, body: ProductCreate):
        return inventory.create_product(conn, user, sku=body.sku, name=body.name, unit=body.unit)

    @app.get("/products/{product_id}/inventory", tags=["inventory"])
    def get_inventory(conn: Conn, _: CurrentUser, product_id: int):
        return inventory.get_inventory(conn, product_id)

    @app.post("/inventory/{product_id}/receive", tags=["inventory"])
    def receive(conn: Conn, user: CurrentUser, product_id: int, body: StockReceive, key: IdempotencyKey = None):
        if key is None:
            return inventory.receive(conn, user, product_id, body.quantity, body.reason)
        result = idempotency.run_idempotent(
            conn,
            user_id=user.id,
            key=key,
            scope="inventory.receive",
            payload={"product_id": product_id, **body.model_dump()},
            operation=lambda: (200, inventory.receive(conn, user, product_id, body.quantity, body.reason)),
        )
        return idempotent_response(result)

    @app.post("/inventory/{product_id}/adjust", tags=["inventory"])
    def adjust(conn: Conn, user: CurrentUser, product_id: int, body: StockAdjust, key: IdempotencyKey = None):
        if key is None:
            return inventory.adjust(conn, user, product_id, body.delta, body.reason)
        result = idempotency.run_idempotent(
            conn,
            user_id=user.id,
            key=key,
            scope="inventory.adjust",
            payload={"product_id": product_id, **body.model_dump()},
            operation=lambda: (200, inventory.adjust(conn, user, product_id, body.delta, body.reason)),
        )
        return idempotent_response(result)

    # ------------------------------------------------------------ reservations
    @app.post("/reservations", tags=["reservations"], status_code=201)
    def create_reservation(conn: Conn, user: CurrentUser, body: ReservationCreate, key: IdempotencyKey = None):
        items_payload = [it.model_dump() for it in body.items] if body.items else None
        result = reservations.create_reservation_idempotent(
            conn,
            user,
            key,
            items=items_payload,
            product_id=body.product_id,
            quantity=body.quantity,
            order_reference=body.order_reference,
            ttl_minutes=body.ttl_minutes,
        )
        return idempotent_response(result)

    @app.get("/reservations", tags=["reservations"])
    def list_reservations(
        conn: Conn,
        user: CurrentUser,
        status: Literal["active", "partially_fulfilled", "fulfilled", "cancelled", "expired"] | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ):
        return reservations.list_reservations(conn, user, status=status, limit=limit)

    @app.post("/reservations/expire", tags=["reservations"])
    def expire_now(conn: Conn, user: CurrentUser):
        require_manager(user)
        return {"expired": reservations.expire_due(conn)}

    @app.get("/reservations/{reservation_id}", tags=["reservations"])
    def get_reservation(conn: Conn, user: CurrentUser, reservation_id: int):
        return reservations.get_reservation(conn, user, reservation_id)

    @app.post("/reservations/{reservation_id}/cancel", tags=["reservations"])
    def cancel(conn: Conn, user: CurrentUser, reservation_id: int, body: TransitionRequest | None = None):
        return reservations.cancel(conn, user, reservation_id, body.reason if body else None)

    @app.post("/reservations/{reservation_id}/fulfill", tags=["reservations"])
    def fulfill(conn: Conn, user: CurrentUser, reservation_id: int, body: TransitionRequest | None = None):
        return reservations.fulfill(conn, user, reservation_id, body.reason if body else None)

    @app.post("/reservations/{reservation_id}/lines/{line_id}/fulfill", tags=["reservations"])
    def fulfill_line(
        conn: Conn,
        user: CurrentUser,
        reservation_id: int,
        line_id: int,
        body: LineTransitionRequest | None = None,
    ):
        qty = body.quantity if body else None
        reason = body.reason if body else None
        return reservations.fulfill_line(conn, user, reservation_id, line_id, quantity=qty, reason=reason)

    # ------------------------------------------------------------ audit
    @app.get("/audit-events", tags=["audit"])
    def list_audit(
        conn: Conn,
        user: CurrentUser,
        product_id: int | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ):
        require_manager(user)
        return audit.list_events(conn, product_id=product_id, limit=limit)

    # ------------------------------------------------------------ demo UI
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
