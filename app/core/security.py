"""Password hashing and token primitives. No database or HTTP code here."""

import asyncio
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import settings
from app.exceptions import Unauthorized

_hasher = PasswordHasher()  # argon2id with the library's recommended parameters


# --- passwords --------------------------------------------------------------------
# argon2 is deliberately slow and CPU-bound (tens of ms). Called directly it would
# block the event loop for that long, stalling every other request on this worker,
# so it always runs in a thread.


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


async def verify_password(password_hash: str, password: str) -> bool:
    try:
        return await asyncio.to_thread(_hasher.verify, password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


@cache
def _dummy_hash() -> str:
    return _hasher.hash("timing-equaliser")


async def burn_password_check(password: str) -> None:
    """Spend the same time as a real verification. Used when the email is unknown, so
    response timing doesn't reveal which emails are registered."""
    await verify_password(_dummy_hash(), password)


# --- access tokens (stateless JWT) ------------------------------------------------


@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID
    session_id: uuid.UUID  # the refresh-token family this access token belongs to


def create_access_token(user_id: uuid.UUID, session_id: uuid.UUID) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "sid": str(session_id),
        "type": "access",
        "iat": now,
        "exp": now + timedelta(seconds=settings.ACCESS_TOKEN_TTL_SECONDS),
    }
    return jwt.encode(
        payload, settings.JWT_SECRET.get_secret_value(), algorithm=settings.JWT_ALGORITHM
    )


def decode_access_token(token: str) -> AccessClaims:
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET.get_secret_value(),
            # Pin the algorithm: never let the token header choose it ("alg": "none").
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["sub", "sid", "type", "iat", "exp"]},
        )
        if payload["type"] != "access":
            raise Unauthorized("Invalid access token")
        return AccessClaims(uuid.UUID(payload["sub"]), uuid.UUID(payload["sid"]))
    except jwt.ExpiredSignatureError:
        raise Unauthorized("Access token expired", code="token_expired") from None
    except (jwt.InvalidTokenError, ValueError):
        raise Unauthorized("Invalid access token") from None


# --- refresh tokens (opaque, stored hashed) ---------------------------------------


def generate_refresh_token() -> str:
    # 256 bits of randomness; opaque, so the only way to use it is via our table.
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    # Plain SHA-256 is enough here (unlike passwords): the input is 256 random bits,
    # so there is nothing to brute-force, and a fast hash keeps the lookup cheap.
    return hashlib.sha256(token.encode()).hexdigest()
