from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import uuid4

import jwt
from jwt import InvalidTokenError


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
    token_jti = uuid4().hex if jti is None else jti
    payload = {
        "sub": user_id,
        "role": role,
        "exp": now + ttl_seconds,
        "iat": now,
        "jti": token_jti,
    }
    return jwt.encode(payload, secret, algorithm="HS256", headers={"typ": "JWT"})


def decode_access_token(token: str, *, secret: str, now: int | None = None) -> TokenClaims:
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"verify_exp": False, "verify_iat": False},
        )
    except InvalidTokenError as exc:
        raise JwtError("invalid token") from exc
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
