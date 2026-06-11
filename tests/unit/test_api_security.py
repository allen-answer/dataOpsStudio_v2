from __future__ import annotations

import pytest

from app.api.security import (
    JwtError,
    bearer_token,
    create_access_token,
    decode_access_token,
)


def test_jwt_roundtrip_hs256() -> None:
    token = create_access_token(
        user_id="user-1",
        role="admin",
        secret="jwt-secret",
        ttl_seconds=60,
        issued_at=100,
        jti="token-1",
    )

    claims = decode_access_token(token, secret="jwt-secret", now=120)

    assert claims.user_id == "user-1"
    assert claims.role == "admin"
    assert claims.expires_at == 160
    assert claims.issued_at == 100
    assert claims.jti == "token-1"


def test_jwt_rejects_bad_signature() -> None:
    token = create_access_token(
        user_id="user-1",
        role="admin",
        secret="jwt-secret",
        ttl_seconds=60,
        issued_at=100,
    )

    with pytest.raises(JwtError):
        decode_access_token(token, secret="other-secret", now=120)


def test_jwt_rejects_expired_token() -> None:
    token = create_access_token(
        user_id="user-1",
        role="admin",
        secret="jwt-secret",
        ttl_seconds=60,
        issued_at=100,
    )

    with pytest.raises(JwtError):
        decode_access_token(token, secret="jwt-secret", now=160)


def test_bearer_token_parses_authorization_header() -> None:
    assert bearer_token("Bearer abc.def.ghi") == "abc.def.ghi"

    with pytest.raises(JwtError):
        bearer_token(None)
