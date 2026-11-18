"""Every error leaves the API as {"error": {"code", "message", "details"}}.

Framework errors (404/405) are checked against the real app. The other handlers are
exercised through a small throwaway app that registers the same handlers, so each
error type can be raised on demand without needing a feature endpoint.
"""

import logging
from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import register_exception_handlers
from app.exceptions import (
    AppError,
    BadRequest,
    Conflict,
    NotFound,
    PreconditionFailed,
    PreconditionRequired,
    RateLimited,
    ServiceUnavailable,
    Unauthorized,
    UnprocessableEntity,
)


def assert_envelope(body: dict, code: str) -> dict:
    assert set(body) == {"error"}
    error = body["error"]
    assert set(error) == {"code", "message", "details"}
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    assert isinstance(error["details"], list)
    return error


# --- framework errors, real app -----------------------------------------------------


async def test_unknown_route_is_404_envelope(client: AsyncClient):
    response = await client.get("/does-not-exist")

    assert response.status_code == 404
    assert_envelope(response.json(), "not_found")


async def test_wrong_method_is_405_envelope_with_allow_header(client: AsyncClient):
    response = await client.post("/health")

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert_envelope(response.json(), "method_not_allowed")


# --- handlers, throwaway app --------------------------------------------------------


class Payload(BaseModel):
    amount: Decimal = Field(gt=0)


def build_app(error: Exception | None = None) -> FastAPI:
    probe = FastAPI()
    register_exception_handlers(probe)

    @probe.post("/validate")
    async def validate(payload: Payload) -> dict:
        return {}

    @probe.get("/raise")
    async def raise_error() -> dict:
        assert error is not None
        raise error

    return probe


async def call(probe: FastAPI, method: str, url: str, **kwargs):
    transport = ASGITransport(app=probe, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        return await http.request(method, url, **kwargs)


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (BadRequest(), 400, "bad_request"),
        (NotFound(), 404, "not_found"),
        (Conflict(), 409, "conflict"),
        (PreconditionFailed(), 412, "precondition_failed"),
        (UnprocessableEntity(), 422, "unprocessable_entity"),
        (PreconditionRequired(), 428, "precondition_required"),
        (ServiceUnavailable(), 503, "service_unavailable"),
    ],
)
async def test_app_errors_map_to_status_and_code(error: AppError, status: int, code: str):
    response = await call(build_app(error), "GET", "/raise")

    assert response.status_code == status
    assert_envelope(response.json(), code)


async def test_app_error_message_code_and_details_can_be_overridden():
    error = NotFound("Expense not found", code="expense_not_found", details=[{"id": "x"}])

    response = await call(build_app(error), "GET", "/raise")

    body = assert_envelope(response.json(), "expense_not_found")
    assert body["message"] == "Expense not found"
    assert body["details"] == [{"id": "x"}]


async def test_rate_limited_sets_retry_after():
    response = await call(build_app(RateLimited(retry_after_seconds=42)), "GET", "/raise")

    assert response.status_code == 429
    assert response.headers["retry-after"] == "42"
    assert_envelope(response.json(), "rate_limited")


async def test_retry_after_is_never_below_one_second():
    response = await call(build_app(RateLimited(retry_after_seconds=0)), "GET", "/raise")

    assert response.headers["retry-after"] == "1"


async def test_unauthorized_advertises_bearer_scheme():
    response = await call(build_app(Unauthorized()), "GET", "/raise")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert_envelope(response.json(), "unauthorized")


async def test_validation_error_is_422_with_field_details_and_no_echoed_input():
    response = await call(build_app(), "POST", "/validate", json={"amount": "-5"})

    assert response.status_code == 422
    error = assert_envelope(response.json(), "validation_error")
    assert error["details"][0]["field"] == "body.amount"
    assert set(error["details"][0]) == {"field", "message", "type"}


async def test_malformed_json_is_422_envelope():
    response = await call(
        build_app(),
        "POST",
        "/validate",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert_envelope(response.json(), "validation_error")


async def test_unknown_http_status_falls_back_to_generic_code():
    response = await call(build_app(StarletteHTTPException(status_code=599)), "GET", "/raise")

    assert response.status_code == 599
    assert_envelope(response.json(), "error")


async def test_unhandled_exception_is_500_without_leaking_internals(caplog):
    with caplog.at_level(logging.ERROR, logger="app.core.errors"):
        response = await call(build_app(RuntimeError("db password is hunter2")), "GET", "/raise")

    assert response.status_code == 500
    error = assert_envelope(response.json(), "internal_error")
    assert "hunter2" not in response.text
    assert error["message"] == "Internal server error"
    # ...but the real cause is logged for us, with the traceback.
    assert "Unhandled error on GET /raise" in caplog.text
