"""Registration, login, refresh-token rotation with reuse detection, logout.

Session model: a login starts a session = a refresh-token family (family_id). Each
refresh marks the presented token used and issues a new one in the same family, so
exactly one token per family is "live" (not used, not revoked). Access tokens carry
the family_id as "sid" and are accepted only while their family has a live token
(see app/core/auth.py), so revoking refresh tokens kills access tokens immediately.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
    burn_password_check,
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.exceptions import Conflict, Unauthorized
from app.models import RefreshToken, User
from app.services.rate_limit_service import LoginRateLimiter


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int


class AuthService:
    def __init__(self, db: AsyncSession, rate_limiter: LoginRateLimiter) -> None:
        self.db = db
        self.rate_limiter = rate_limiter

    async def register(self, email: str, password: str, base_currency: str) -> User:
        user = User(
            email=email.lower(),
            password_hash=await hash_password(password),
            base_currency=base_currency,
        )
        self.db.add(user)
        try:
            await self.db.commit()
        except IntegrityError:
            # The unique constraint is the real duplicate check: a "SELECT then INSERT"
            # pre-check would still race with a concurrent registration.
            await self.db.rollback()
            raise Conflict("Email is already registered", code="email_taken") from None
        return user

    async def login(self, email: str, password: str) -> TokenPair:
        email = email.lower()
        await self.rate_limiter.register_attempt(email)  # raises 429 past the limit

        user = await self.db.scalar(select(User).where(User.email == email))
        if user is None:
            await burn_password_check(password)  # same timing as a real check
            raise Unauthorized("Invalid email or password", code="invalid_credentials")
        if not await verify_password(user.password_hash, password):
            raise Unauthorized("Invalid email or password", code="invalid_credentials")

        await self.rate_limiter.reset(email)
        pair = self._issue(user.id, family_id=uuid.uuid4())
        await self.db.commit()
        return pair

    async def refresh(self, refresh_token: str) -> TokenPair:
        # FOR UPDATE: two concurrent refreshes with the same token are serialised; the
        # second sees used_at set by the first and is treated as a reuse.
        token = await self.db.scalar(
            select(RefreshToken)
            .where(RefreshToken.token_hash == hash_refresh_token(refresh_token))
            .with_for_update()
        )
        if token is None:
            raise Unauthorized("Invalid refresh token", code="invalid_refresh_token")

        now = datetime.now(UTC)
        if token.used_at is not None:
            # An already-rotated token came back: someone else holds a copy. Revoke
            # every token of this user (all sessions), and commit that *before*
            # raising — otherwise the error would roll the revocation back.
            await self.db.execute(
                update(RefreshToken)
                .where(RefreshToken.user_id == token.user_id, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            await self.db.commit()
            raise Unauthorized(
                "Refresh token was already used; all sessions have been revoked",
                code="refresh_token_reused",
            )
        if token.revoked_at is not None:
            raise Unauthorized("Refresh token has been revoked", code="refresh_token_revoked")
        if token.expires_at <= now:
            raise Unauthorized("Refresh token has expired", code="refresh_token_expired")

        token.used_at = now
        pair = self._issue(token.user_id, family_id=token.family_id)
        await self.db.commit()
        return pair

    async def logout(self, refresh_token: str) -> None:
        """End the session the token belongs to: revoke its whole family, which also
        invalidates that session's access token. Idempotent."""
        token = await self.db.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(refresh_token))
        )
        if token is None:
            raise Unauthorized("Invalid refresh token", code="invalid_refresh_token")
        await self.db.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == token.family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        await self.db.commit()

    def _issue(self, user_id: uuid.UUID, family_id: uuid.UUID) -> TokenPair:
        """Add a new refresh-token row (caller commits) and build the token pair."""
        refresh_token = generate_refresh_token()
        self.db.add(
            RefreshToken(
                user_id=user_id,
                family_id=family_id,
                token_hash=hash_refresh_token(refresh_token),
                expires_at=datetime.now(UTC)
                + timedelta(seconds=settings.REFRESH_TOKEN_TTL_SECONDS),
            )
        )
        return TokenPair(
            access_token=create_access_token(user_id, session_id=family_id),
            refresh_token=refresh_token,
            expires_in=settings.ACCESS_TOKEN_TTL_SECONDS,
        )
