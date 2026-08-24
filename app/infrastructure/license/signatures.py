"""Ed25519 verification kept inside the R3-approved license cryptography seam."""

from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: TID251
    Ed25519PublicKey,
)


def verify_ed25519_signature(*, public_key: bytes, signature: bytes, message: bytes) -> None:
    """Verify a detached signature and normalize all invalid inputs to ``ValueError``."""

    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except (InvalidSignature, ValueError) as exc:
        raise ValueError("invalid Ed25519 signature") from exc


__all__ = ["verify_ed25519_signature"]
