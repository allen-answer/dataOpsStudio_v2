from __future__ import annotations

from app.infrastructure.secretstore.totp import (
    accepted_totp_counter,
    generate_recovery_codes,
    generate_totp_seed,
    provisioning_uri,
    totp_code_for_test,
    verify_totp_code,
)


def test_totp_verify_accepts_current_code() -> None:
    secret = generate_totp_seed()
    now = 1_700_000_000
    code = totp_code_for_test(secret=secret, now=now)

    assert verify_totp_code(secret=secret, code=code, now=now) is True
    assert verify_totp_code(secret=secret, code="000000", now=now, window=0) is False


def test_totp_returns_accepted_counter() -> None:
    secret = generate_totp_seed()
    now = 1_700_000_000
    code = totp_code_for_test(secret=secret, now=now)

    assert accepted_totp_counter(secret=secret, code=code, now=now) == now // 30
    assert accepted_totp_counter(secret=secret, code="000000", now=now, window=0) is None


def test_provisioning_uri_contains_issuer_and_secret() -> None:
    secret = generate_totp_seed()
    uri = provisioning_uri(secret=secret, username="user-1")

    assert uri.startswith("otpauth://totp/")
    assert f"secret={secret}" in uri
    assert "issuer=DataOpsStudio" in uri


def test_generate_recovery_codes_returns_eight_printable_codes() -> None:
    codes = generate_recovery_codes()

    assert len(codes) == 8
    assert len(set(codes)) == 8
    assert all("-" in code for code in codes)
