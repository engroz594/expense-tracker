import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.enums import Currency


class RegisterRequest(BaseModel):
    email: EmailStr
    # Upper bound so a huge password can't be used to burn server CPU in argon2.
    password: str = Field(min_length=8, max_length=128)
    base_currency: Currency


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=512)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    base_currency: Currency
    created_at: datetime


class TokenPairResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # access token lifetime, seconds
