"""get_current_user: the dependency every protected endpoint uses."""

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_access_token
from app.exceptions import Unauthorized
from app.models import RefreshToken, User

# auto_error=False: a missing header goes through our Unauthorized (error envelope),
# not FastAPI's default 403 response.
_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    if credentials is None:
        raise Unauthorized("Missing bearer token")
    claims = decode_access_token(credentials.credentials)

    # One query: the user exists AND the token's session still has a live refresh
    # token. Logout or a reuse cascade revokes the family, so access tokens of that
    # session stop working immediately instead of living out their 15 minutes.
    # Served by the partial index ix_refresh_tokens_live_family_id.
    session_is_live = exists().where(
        RefreshToken.family_id == claims.session_id,
        RefreshToken.user_id == User.id,
        RefreshToken.used_at.is_(None),
        RefreshToken.revoked_at.is_(None),
    )
    user = await db.scalar(select(User).where(User.id == claims.user_id, session_is_live))
    if user is None:
        raise Unauthorized("Session is no longer valid", code="session_revoked")
    return user
