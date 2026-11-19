import uuid

import pytest

from app.core.security import (
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.exceptions import Unauthorized


async def test_password_hash_round_trip():
    hashed = await hash_password("s3cret-passw0rd")

    assert await verify_password(hashed, "s3cret-passw0rd")
    assert not await verify_password(hashed, "wrong")


async def test_verify_against_a_corrupt_hash_is_false_not_an_error():
    assert not await verify_password("not-an-argon2-hash", "anything")


async def test_access_token_round_trip():
    user_id, session_id = uuid.uuid4(), uuid.uuid4()

    claims = decode_access_token(create_access_token(user_id, session_id))

    assert (claims.user_id, claims.session_id) == (user_id, session_id)


async def test_token_with_wrong_type_is_rejected():
    import jwt

    from app.core.config import settings

    token = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "sid": str(uuid.uuid4()),
            "type": "refresh",
            "iat": 0,
            "exp": 4_102_444_800,
        },
        settings.JWT_SECRET.get_secret_value(),
        algorithm="HS256",
    )

    with pytest.raises(Unauthorized):
        decode_access_token(token)


async def test_refresh_tokens_are_random_and_hash_deterministically():
    a, b = generate_refresh_token(), generate_refresh_token()

    assert a != b
    assert len(a) >= 43  # 32 random bytes, url-safe base64
    assert hash_refresh_token(a) == hash_refresh_token(a)
    assert len(hash_refresh_token(a)) == 64
