from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote


def generate_totp_seed() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def provisioning_uri(*, secret: str, username: str, issuer: str = "DataOpsStudio") -> str:
    label = f"{issuer}:{username}"
    return (
        f"otpauth://totp/{quote(label)}"
        f"?secret={quote(secret)}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"
    )


def verify_totp_code(
    *,
    secret: str,
    code: str,
    now: int | None = None,
    window: int = 1,
) -> bool:
    if not code.isdigit() or len(code) != 6:
        return False
    current = int(time.time()) if now is None else now
    counter = current // 30
    for offset in range(-window, window + 1):
        expected = _totp_at(secret, counter + offset)
        if hmac.compare_digest(expected, code):
            return True
    return False


def totp_code_for_test(*, secret: str, now: int) -> str:
    return _totp_at(secret, now // 30)


def generate_recovery_codes(count: int = 8) -> list[str]:
    return [f"{secrets.token_hex(4)}-{secrets.token_hex(4)}" for _ in range(count)]


def _totp_at(secret: str, counter: int) -> str:
    key = _decode_base32(secret)
    message = struct.pack(">Q", counter)
    digest = hmac.new(key, message, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def _decode_base32(secret: str) -> bytes:
    normalized = secret.strip().replace(" ", "").upper()
    padding = "=" * (-len(normalized) % 8)
    return base64.b32decode(normalized + padding)
