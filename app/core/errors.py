"""Exception handlers: every error leaves the API in one shape.

    {"error": {"code": "...", "message": "...", "details": [...]}}

Four sources of errors are covered:
  1. AppError            — errors we raise on purpose (404, 409, 412, 429, ...).
  2. RequestValidationError — FastAPI/Pydantic input validation (422).
  3. Starlette HTTPException — raised by the framework itself, e.g. unknown route (404)
     or wrong method (405). We register Starlette's class, not FastAPI's subclass,
     otherwise those framework errors would bypass the envelope.
  4. Any other Exception — a bug. Logged with its traceback; the client gets a
     generic 500 with no internals leaked.
"""

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.exceptions import AppError

logger = logging.getLogger(__name__)


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: list[Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details or []}},
        headers=headers,
    )


async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    return error_response(exc.status_code, exc.code, exc.message, exc.details, exc.headers)


async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Keep only loc/msg/type: Pydantic's "ctx" can hold non-JSON values (e.g. Decimal,
    # exception instances) and "input" would echo back whatever the client sent.
    details = [
        {
            "field": ".".join(str(part) for part in err["loc"]),
            "message": err["msg"],
            "type": err["type"],
        }
        for err in exc.errors()
    ]
    return error_response(422, "validation_error", "Request validation failed", details)


async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # Derive a snake_case code from the status phrase: 404 -> "not_found",
    # 405 -> "method_not_allowed".
    try:
        phrase = HTTPStatus(exc.status_code).phrase
    except ValueError:  # non-standard status, e.g. 599
        phrase = "Error"
    code = phrase.lower().replace(" ", "_").replace("-", "_")
    # Starlette sets detail to "" for statuses without a standard phrase; the envelope
    # promises a non-empty message, so fall back to the phrase.
    message = exc.detail if isinstance(exc.detail, str) and exc.detail else phrase
    return error_response(exc.status_code, code, message, headers=exc.headers)


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return error_response(500, "internal_error", "Internal server error")


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(Exception, handle_unexpected_error)
