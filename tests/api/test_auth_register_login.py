from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.models import RefreshToken, User
from tests.helpers import DEFAULT_PASSWORD

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"


async def test_register_returns_201_with_public_fields_only(client: AsyncClient, db: AsyncSession):
    response = await client.post(
        REGISTER,
        json={"email": "Bob@Example.com", "password": DEFAULT_PASSWORD, "base_currency": "AED"},
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "email", "base_currency", "created_at"}
    assert body["email"] == "bob@example.com"  # normalised
    assert body["base_currency"] == "AED"
    user = await db.scalar(select(User))
    assert user.password_hash.startswith("$argon2id$")  # hashed, never plaintext
    assert DEFAULT_PASSWORD not in user.password_hash


async def test_register_duplicate_email_is_409_case_insensitively(
    client: AsyncClient, register_user
):
    await register_user(email="alice@example.com")

    response = await client.post(
        REGISTER,
        json={"email": "ALICE@example.com", "password": DEFAULT_PASSWORD, "base_currency": "PKR"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_taken"


async def test_register_rejects_invalid_input(client: AsyncClient):
    cases = [
        {"email": "not-an-email", "password": DEFAULT_PASSWORD, "base_currency": "PKR"},
        {"email": "a@example.com", "password": "short", "base_currency": "PKR"},
        {"email": "a@example.com", "password": DEFAULT_PASSWORD, "base_currency": "GBP"},
    ]
    for payload in cases:
        response = await client.post(REGISTER, json=payload)
        assert response.status_code == 422, payload
        assert response.json()["error"]["code"] == "validation_error"


async def test_login_returns_token_pair_and_starts_a_session(
    client: AsyncClient, db: AsyncSession, register_user
):
    user = await register_user()

    response = await client.post(
        LOGIN, json={"email": "ALICE@example.com", "password": DEFAULT_PASSWORD}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 15 * 60
    claims = decode_access_token(body["access_token"])
    assert str(claims.user_id) == user["id"]
    stored = await db.scalar(select(RefreshToken))
    assert stored.family_id == claims.session_id
    assert stored.token_hash != body["refresh_token"]  # only the hash is stored


async def test_wrong_password_and_unknown_email_get_the_same_401(
    client: AsyncClient, register_user
):
    await register_user()

    wrong_password = await client.post(
        LOGIN, json={"email": "alice@example.com", "password": "wrong-password"}
    )
    unknown_email = await client.post(
        LOGIN, json={"email": "nobody@example.com", "password": "wrong-password"}
    )

    for response in (wrong_password, unknown_email):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_credentials"
    # Same message too: the response must not reveal which emails exist.
    assert wrong_password.json() == unknown_email.json()
