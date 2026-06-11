from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4


class JwtError(ValueError):
    """JWT is missing, malformed, expired, or has an invalid signature."""


@dataclass(frozen=True)
class TokenClaims:
    user_id: str
    role: str
    expires_at: int
    issued_at: int
    jti: str | None = None


@dataclass(frozen=True)
class CurrentUser:
    id: str
    role: str


def create_access_token(
    *,
    user_id: str,
    role: str,
    secret: str,
    ttl_seconds: int = 3600,
    issued_at: int | None = None,
    jti: str | None = None,
) -> str:
    now = int(time.time()) if issued_at is None else issued_at
    header = {"alg": "HS256", "typ": "JWT"}
    token_jti = uuid4().hex if jti is None else jti
    payload = {
        "sub": user_id,
        "role": role,
        "exp": now + ttl_seconds,
        "iat": now,
        "jti": token_jti,
    }
    signing_input = f"{_b64_json(header)}.{_b64_json(payload)}"
    signature = hmac.new(
        secret.encode("utf-8"),
        signing_input.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{signing_input}.{_b64_encode(signature)}"


def decode_access_token(token: str, *, secret: str, now: int | None = None) -> TokenClaims:
    parts = token.split(".")
    if len(parts) != 3:
        raise JwtError("malformed token")
    signing_input = f"{parts[0]}.{parts[1]}"
    expected = hmac.new(
        secret.encode("utf-8"),
        signing_input.encode("ascii"),
        hashlib.sha256,
    ).digest()
    actual = _b64_decode(parts[2])
    if not hmac.compare_digest(expected, actual):
        raise JwtError("invalid token signature")

    payload = _json_from_b64(parts[1])
    exp = payload.get("exp")
    iat = payload.get("iat", 0)
    jti = payload.get("jti")
    sub = payload.get("sub")
    role = payload.get("role", "viewer")
    if (
        not isinstance(exp, int)
        or not isinstance(iat, int)
        or not isinstance(sub, str)
        or not isinstance(role, str)
    ):
        raise JwtError("invalid token claims")
    if jti is not None and not isinstance(jti, str):
        raise JwtError("invalid token claims")
    current = int(time.time()) if now is None else now
    if exp <= current:
        raise JwtError("token expired")
    return TokenClaims(user_id=sub, role=role, expires_at=exp, issued_at=iat, jti=jti)


def bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise JwtError("missing authorization header")
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise JwtError("invalid authorization header")
    token = authorization[len(prefix) :].strip()
    if not token:
        raise JwtError("missing bearer token")
    return token


def _b64_json(payload: dict[str, Any]) -> str:
    return _b64_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def _json_from_b64(value: str) -> dict[str, Any]:
    payload = json.loads(_b64_decode(value).decode("utf-8"))
    if not isinstance(payload, dict):
        raise JwtError("invalid token payload")
    return payload


def _b64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except ValueError as exc:
        raise JwtError("invalid base64") from exc
