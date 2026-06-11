from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping

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


def test_jwt_decodes_legacy_handwritten_hs256_token() -> None:
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "exp": 160,
        "iat": 100,
        "jti": "legacy-token-1",
        "role": "admin",
        "sub": "user-1",
    }
    signing_input = f"{_legacy_b64_json(header)}.{_legacy_b64_json(payload)}"
    signature = hmac.new(
        b"jwt-secret",
        signing_input.encode("ascii"),
        hashlib.sha256,
    ).digest()
    token = f"{signing_input}.{_legacy_b64_encode(signature)}"

    claims = decode_access_token(token, secret="jwt-secret", now=120)

    assert claims.user_id == "user-1"
    assert claims.role == "admin"
    assert claims.expires_at == 160
    assert claims.issued_at == 100
    assert claims.jti == "legacy-token-1"


def test_bearer_token_parses_authorization_header() -> None:
    assert bearer_token("Bearer abc.def.ghi") == "abc.def.ghi"

    with pytest.raises(JwtError):
        bearer_token(None)


def _legacy_b64_json(payload: Mapping[str, object]) -> str:
    return _legacy_b64_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )


def _legacy_b64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
