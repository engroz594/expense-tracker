"""Refresh rotation, reuse detection, logout, and get_current_user."""

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.models import RefreshToken, User
from tests.helpers import bearer

REFRESH = "/api/v1/auth/refresh"
LOGOUT = "/api/v1/auth/logout"


@pytest.fixture
async def protected():
    """A client for a probe app with one endpoint behind get_current_user. (The real
    protected endpoints arrive with expenses; this tests the dependency itself.)"""
    probe = FastAPI()
    register_exception_handlers(probe)

    @probe.get("/me")
    async def me(user: User = Depends(get_current_user)) -> dict:
        return {"id": str(user.id)}

    transport = ASGITransport(app=probe)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def refresh(client: AsyncClient, token: str):
    return await client.post(REFRESH, json={"refresh_token": token})


# --- rotation -------------------------------------------------------------------------


async def test_refresh_rotates_to_a_new_pair_in_the_same_session(
    client: AsyncClient, protected: AsyncClient, register_user, login
):
    await register_user()
    first = await login()

    response = await refresh(client, first["refresh_token"])

    assert response.status_code == 200
    second = response.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert (await protected.get("/me", headers=bearer(second))).status_code == 200
    # Same session, so the earlier access token is still valid until it expires.
    assert (await protected.get("/me", headers=bearer(first))).status_code == 200


async def test_using_a_refresh_token_twice_revokes_all_tokens(
    client: AsyncClient, protected: AsyncClient, db: AsyncSession, register_user, login
):
    """Brief: if an old refresh token is used again, revoke all of the user's tokens."""
    await register_user()
    phone = await login()
    laptop = await login()  # a second, independent session of the same user
    rotated = (await refresh(client, phone["refresh_token"])).json()

    # The already-used token comes back: theft.
    reuse = await refresh(client, phone["refresh_token"])

    assert reuse.status_code == 401
    assert reuse.json()["error"]["code"] == "refresh_token_reused"
    # Every refresh token of the user is dead — the rotated one and the other session's...
    for token in (rotated["refresh_token"], laptop["refresh_token"]):
        assert (await refresh(client, token)).status_code == 401
    # ...and so are all access tokens, immediately (not after their 15 minutes).
    for tokens in (phone, rotated, laptop):
        response = await protected.get("/me", headers=bearer(tokens))
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "session_revoked"
    live = await db.scalar(select(RefreshToken).where(RefreshToken.revoked_at.is_(None)))
    assert live is None


async def test_refresh_with_unknown_token_is_401(client: AsyncClient):
    response = await refresh(client, "not-a-real-token")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_refresh_token"


async def test_expired_refresh_token_is_401(
    client: AsyncClient, db: AsyncSession, register_user, login
):
    await register_user()
    tokens = await login()
    await db.execute(
        update(RefreshToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await db.commit()

    response = await refresh(client, tokens["refresh_token"])

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "refresh_token_expired"


# --- logout ---------------------------------------------------------------------------


async def test_logout_ends_only_that_session_immediately(
    client: AsyncClient, protected: AsyncClient, register_user, login
):
    await register_user()
    phone = await login()
    laptop = await login()

    response = await client.post(LOGOUT, json={"refresh_token": phone["refresh_token"]})

    assert response.status_code == 204
    revoked = await refresh(client, phone["refresh_token"])
    assert revoked.status_code == 401
    assert revoked.json()["error"]["code"] == "refresh_token_revoked"
    assert (await protected.get("/me", headers=bearer(phone))).status_code == 401
    # The other session is untouched.
    assert (await protected.get("/me", headers=bearer(laptop))).status_code == 200
    assert (await refresh(client, laptop["refresh_token"])).status_code == 200


async def test_logout_is_idempotent_and_rejects_unknown_tokens(
    client: AsyncClient, register_user, login
):
    await register_user()
    tokens = await login()

    first = await client.post(LOGOUT, json={"refresh_token": tokens["refresh_token"]})
    again = await client.post(LOGOUT, json={"refresh_token": tokens["refresh_token"]})
    unknown = await client.post(LOGOUT, json={"refresh_token": "not-a-real-token"})

    assert (first.status_code, again.status_code, unknown.status_code) == (204, 204, 401)


# --- get_current_user -----------------------------------------------------------------


async def test_missing_bearer_token_is_401_with_challenge(protected: AsyncClient):
    response = await protected.get("/me")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "token",
    [
        "garbage",
        # Correct shape, wrong signing key.
        jwt.encode({"sub": "x", "sid": "y", "type": "access"}, "another-key-0123456789abcdef0123"),
    ],
)
async def test_malformed_or_forged_access_token_is_401(protected: AsyncClient, token: str):
    response = await protected.get("/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


async def test_expired_access_token_is_401_token_expired(
    protected: AsyncClient, register_user, login, monkeypatch: pytest.MonkeyPatch
):
    await register_user()
    monkeypatch.setattr(settings, "ACCESS_TOKEN_TTL_SECONDS", -1)
    tokens = await login()

    response = await protected.get("/me", headers=bearer(tokens))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "token_expired"


async def test_refresh_token_is_not_accepted_as_an_access_token(
    protected: AsyncClient, register_user, login
):
    await register_user()
    tokens = await login()

    response = await protected.get(
        "/me", headers={"Authorization": f"Bearer {tokens['refresh_token']}"}
    )

    assert response.status_code == 401


async def test_access_token_lives_15_minutes(register_user, login):
    await register_user()
    tokens = await login()

    claims = jwt.decode(tokens["access_token"], options={"verify_signature": False})

    assert claims["exp"] - claims["iat"] == 15 * 60
    assert claims["type"] == "access"
