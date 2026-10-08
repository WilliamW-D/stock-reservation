"""Domain errors. The API layer maps each one to an HTTP status and error code."""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class AuthenticationError(DomainError):
    status_code = 401
    code = "unauthenticated"


class PermissionDenied(DomainError):
    status_code = 403
    code = "forbidden"


class NotFound(DomainError):
    status_code = 404
    code = "not_found"


class InsufficientStock(DomainError):
    status_code = 409
    code = "insufficient_stock"


class InvalidTransition(DomainError):
    status_code = 409
    code = "invalid_transition"


class IdempotencyConflict(DomainError):
    status_code = 409
    code = "idempotency_key_reused"


class InvalidRequest(DomainError):
    status_code = 422
    code = "invalid_request"
