from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.schemas.auth_schemas import (
    LoginRequest,
    RefreshTokenRequest,
    RegisterRequest,
    TokenPairResponse,
    UserResponse,
)
from app.services.auth_service import AuthService, TokenPair
from app.services.rate_limit_service import LoginRateLimiter, get_login_rate_limiter

router = APIRouter()


def get_auth_service(
    db: AsyncSession = Depends(get_db),
    rate_limiter: LoginRateLimiter = Depends(get_login_rate_limiter),
) -> AuthService:
    return AuthService(db, rate_limiter)


def _token_response(pair: TokenPair) -> TokenPairResponse:
    return TokenPairResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
    )


@router.post("/register", status_code=status.HTTP_201_CREATED, response_model=UserResponse)
async def register(
    payload: RegisterRequest, auth: AuthService = Depends(get_auth_service)
) -> UserResponse:
    user = await auth.register(payload.email, payload.password, payload.base_currency)
    return UserResponse.model_validate(user)


@router.post("/login", response_model=TokenPairResponse)
async def login(
    payload: LoginRequest, auth: AuthService = Depends(get_auth_service)
) -> TokenPairResponse:
    return _token_response(await auth.login(payload.email, payload.password))


@router.post("/refresh", response_model=TokenPairResponse)
async def refresh(
    payload: RefreshTokenRequest, auth: AuthService = Depends(get_auth_service)
) -> TokenPairResponse:
    return _token_response(await auth.refresh(payload.refresh_token))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    payload: RefreshTokenRequest, auth: AuthService = Depends(get_auth_service)
) -> Response:
    await auth.logout(payload.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
