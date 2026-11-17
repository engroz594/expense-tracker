"""Application exceptions.

Every error the API returns on purpose is an AppError subclass. Each one carries
its HTTP status and a default machine-readable code; the handler in
app/core/errors.py turns it into the single error envelope:

    {"error": {"code": "...", "message": "...", "details": [...]}}

Services raise these directly (e.g. `raise NotFound("Expense not found")`) and
never build HTTP responses themselves.
"""

from typing import Any


class AppError(Exception):
    """Root of the application's exception hierarchy."""

    status_code: int = 500
    code: str = "internal_error"
    message: str = "Internal server error"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: list[Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.details = details or []
        self.headers = headers
        super().__init__(self.message)


class BadRequest(AppError):
    status_code = 400
    code = "bad_request"
    message = "Bad request"


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"
    message = "Invalid or missing credentials"

    def __init__(self, message: str | None = None, **kwargs: Any) -> None:
        # RFC 6750: a 401 for bearer-token auth should say which scheme is expected.
        kwargs.setdefault("headers", {"WWW-Authenticate": "Bearer"})
        super().__init__(message, **kwargs)


class NotFound(AppError):
    status_code = 404
    code = "not_found"
    message = "Resource not found"


class Conflict(AppError):
    status_code = 409
    code = "conflict"
    message = "Resource already exists"


class PreconditionFailed(AppError):
    status_code = 412
    code = "precondition_failed"
    message = "The resource was modified by someone else"


class UnprocessableEntity(AppError):
    status_code = 422
    code = "unprocessable_entity"
    message = "The request could not be processed"


class PreconditionRequired(AppError):
    status_code = 428
    code = "precondition_required"
    message = "This request must be conditional"


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"
    message = "Too many requests"

    def __init__(self, retry_after_seconds: int, message: str | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("headers", {"Retry-After": str(max(retry_after_seconds, 1))})
        super().__init__(message, **kwargs)


class ServiceUnavailable(AppError):
    status_code = 503
    code = "service_unavailable"
    message = "Service unavailable"
