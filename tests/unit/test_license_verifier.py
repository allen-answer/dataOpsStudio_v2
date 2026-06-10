from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.domain.license import LicenseMode
from app.infrastructure.license.verifier import read_license_limits, verify_license


def test_missing_license_is_trial_30day(tmp_path: Path) -> None:
    now = datetime(2026, 6, 10, tzinfo=UTC)

    state = verify_license(tmp_path / "missing.lic", now=now)

    assert state.mode is LicenseMode.TRIAL
    assert state.expires_at == now + timedelta(days=30)


def test_valid_license_returns_payload_state(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    now = datetime(2026, 6, 10, tzinfo=UTC)
    path = _write_license(
        tmp_path,
        key,
        expires_at=now + timedelta(days=30),
        features=["sql_workspace"],
    )

    state = verify_license(path, now=now, public_key=key.public_key())

    assert state.mode is LicenseMode.VALID
    assert state.edition == "portable"
    assert state.customer == "test-customer"
    assert state.features == ["sql_workspace"]


def test_expired_within_seven_days_returns_in_grace(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    now = datetime(2026, 6, 10, tzinfo=UTC)
    path = _write_license(tmp_path, key, expires_at=now - timedelta(days=3))

    state = verify_license(path, now=now, public_key=key.public_key())

    assert state.mode is LicenseMode.IN_GRACE
    assert state.repair_reason is None


def test_expired_after_grace_returns_repair_expired(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    now = datetime(2026, 6, 10, tzinfo=UTC)
    path = _write_license(tmp_path, key, expires_at=now - timedelta(days=8, seconds=1))

    state = verify_license(path, now=now, public_key=key.public_key())

    assert state.mode is LicenseMode.REPAIR
    assert state.repair_reason == "expired"


def test_bad_signature_returns_repair_invalid_signature(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    wrong_key = Ed25519PrivateKey.generate()
    now = datetime(2026, 6, 10, tzinfo=UTC)
    path = _write_license(tmp_path, key, expires_at=now + timedelta(days=30))

    state = verify_license(path, now=now, public_key=wrong_key.public_key())

    assert state.mode is LicenseMode.REPAIR
    assert state.repair_reason == "invalid_signature"


def test_damaged_license_files_enter_repair_instead_of_crashing(tmp_path: Path) -> None:
    now = datetime(2026, 6, 10, tzinfo=UTC)
    cases = {
        "bom": b"\xef\xbb\xbf{}",
        "truncated": b'{"payload":',
        "newline_signature": json.dumps({"payload": {}, "signature": "not base64\n"}).encode(),
    }
    for name, content in cases.items():
        path = tmp_path / f"{name}.lic"
        path.write_bytes(content)

        state = verify_license(path, now=now)

        assert state.mode is LicenseMode.REPAIR
        assert state.repair_reason == "invalid_signature"


def test_read_license_limits_returns_payload_limits(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    now = datetime(2026, 6, 10, tzinfo=UTC)
    path = _write_license(tmp_path, key, expires_at=now + timedelta(days=30))

    assert read_license_limits(path) == {"max_users": 5}


def _write_license(
    tmp_path: Path,
    key: Ed25519PrivateKey,
    *,
    expires_at: datetime,
    features: list[str] | None = None,
) -> Path:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "license_id": "DOS-TEST",
        "customer": "test-customer",
        "edition": "portable",
        "issued_at": "2026-01-01T00:00:00Z",
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        "features": features or [],
        "limits": {"max_users": 5},
        "offline": True,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    signature = base64.b64encode(key.sign(canonical.encode("utf-8"))).decode("ascii")
    path = tmp_path / "license.lic"
    path.write_text(
        json.dumps({"payload": payload, "signature": signature}),
        encoding="utf-8",
    )
    return path
