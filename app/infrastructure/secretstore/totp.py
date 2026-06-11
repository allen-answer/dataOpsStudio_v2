from __future__ import annotations

import secrets
import time
from datetime import UTC, datetime

import pyotp


def generate_totp_seed() -> str:
    return pyotp.random_base32()


def provisioning_uri(*, secret: str, username: str, issuer: str = "DataOpsStudio") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def verify_totp_code(
    *,
    secret: str,
    code: str,
    now: int | None = None,
    window: int = 1,
) -> bool:
    return accepted_totp_counter(secret=secret, code=code, now=now, window=window) is not None


def accepted_totp_counter(
    *,
    secret: str,
    code: str,
    now: int | None = None,
    window: int = 1,
) -> int | None:
    if not code.isdigit() or len(code) != 6:
        return None
    current = int(time.time()) if now is None else now
    current_counter = current // 30
    totp = pyotp.TOTP(secret)
    for offset in range(-window, window + 1):
        counter = current_counter + offset
        if counter < 0:
            continue
        if totp.verify(
            code,
            for_time=datetime.fromtimestamp(counter * 30, UTC),
            valid_window=0,
        ):
            return counter
    return None


def totp_code_for_test(*, secret: str, now: int) -> str:
    return str(pyotp.TOTP(secret).at(now))


def generate_recovery_codes(count: int = 8) -> list[str]:
    return [f"{secrets.token_hex(4)}-{secrets.token_hex(4)}" for _ in range(count)]
