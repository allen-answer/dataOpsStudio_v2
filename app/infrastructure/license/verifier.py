from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature

# R3 contract seam:license verification may use cryptography.hazmat.
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: TID251
    Ed25519PublicKey,
)

from app.domain.license import LicenseMode, LicenseState

_DEFAULT_PUBLIC_KEY_B64 = "CCHF/VHbmFskHiC9goucp6V9tPSk8LCcon048aOiPSA="
_TRIAL_DAYS = 30
_GRACE_DAYS = 7


def verify_license(
    path: Path = Path("config/license.lic"),
    *,
    now: datetime | None = None,
    public_key: Ed25519PublicKey | None = None,
) -> LicenseState:
    """Verify config/license.lic without network access.

    Invalid, truncated, BOM-prefixed, or signature-failing files enter Repair Mode
    instead of preventing startup.
    """

    current = _utc(now)
    if not path.exists():
        return _trial_30day(current)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        payload = data["payload"]
        if not isinstance(payload, dict):
            raise ValueError("license payload must be an object")
        signature = base64.b64decode(data["signature"], validate=True)
        verifier = public_key or _default_public_key()
        verifier.verify(signature, _canonical_json(payload).encode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return _repair("invalid_signature")
    except InvalidSignature:
        return _repair("invalid_signature")

    try:
        expires = _parse_iso8601(str(payload["expires_at"]))
    except (KeyError, ValueError):
        return _repair("invalid_signature")

    if current > expires:
        if current <= expires + timedelta(days=_GRACE_DAYS):
            return _state_from_payload(LicenseMode.IN_GRACE, payload, expires)
        return _repair("expired")
    return _state_from_payload(LicenseMode.VALID, payload, expires)


def read_license_limits(path: Path = Path("config/license.lic")) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        payload = data["payload"]
        if not isinstance(payload, dict):
            return {}
        limits = payload.get("limits", {})
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError):
        return {}
    return dict(limits) if isinstance(limits, dict) else {}


def _trial_30day(now: datetime) -> LicenseState:
    return LicenseState(
        mode=LicenseMode.TRIAL,
        edition="trial",
        customer=None,
        expires_at=now + timedelta(days=_TRIAL_DAYS),
        features=[],
    )


def _repair(reason: str) -> LicenseState:
    return LicenseState(mode=LicenseMode.REPAIR, repair_reason=reason)


def _state_from_payload(
    mode: LicenseMode,
    payload: dict[str, Any],
    expires_at: datetime,
) -> LicenseState:
    raw_features = payload.get("features", [])
    features = [str(item) for item in raw_features] if isinstance(raw_features, list) else []
    return LicenseState(
        mode=mode,
        edition=_optional_str(payload.get("edition")),
        customer=_optional_str(payload.get("customer")),
        expires_at=expires_at,
        features=features,
    )


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _default_public_key() -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(_DEFAULT_PUBLIC_KEY_B64))


def _parse_iso8601(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("expires_at must be timezone-aware")
    return parsed.astimezone(UTC)


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _optional_str(value: object) -> str | None:
    return str(value) if value is not None else None
