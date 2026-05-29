from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any, cast

import pytest

from app.domain.secret import SecretKind, SecretRef
from app.infrastructure.bootstrap.protocol import BootstrapSecrets
from app.infrastructure.secretstore.local_file import (
    LocalFileSecretStore,
    SecretAuditContext,
    SecretKindRejectedError,
    _SecretRecord,
    _SecretStorage,
)


def test_store_secret_rejects_pg_bootstrap_kinds_before_storage_write() -> None:
    storage = _NoopStorage()
    store = LocalFileSecretStore(
        storage,
        _StaticBootstrapSecrets(_fernet_key()),
    )

    bad_kinds: list[Any] = [
        "PG_APP_USER_PASSWORD",
        "PG_SUPERUSER_PASSWORD",
        "pg_app_password",
        "pg_superuser_password",
        _FakeKind("pg_app_user_password"),
    ]

    for bad_kind in bad_kinds:
        with pytest.raises(SecretKindRejectedError):
            store.store_secret("must-not-store", cast(SecretKind, bad_kind))

    assert storage.insert_calls == 0


def test_store_secret_rejects_string_application_kind_casts() -> None:
    storage = _NoopStorage()
    store = LocalFileSecretStore(
        storage,
        _StaticBootstrapSecrets(_fernet_key()),
    )

    with pytest.raises(TypeError):
        store.store_secret(
            "must-not-store",
            cast(SecretKind, SecretKind.DATASOURCE_PASSWORD.value),
        )

    assert storage.insert_calls == 0


class _StaticBootstrapSecrets(BootstrapSecrets):
    def __init__(self, master_key: bytes) -> None:
        self._master_key = master_key

    def get_master_key(self) -> bytes:
        return self._master_key

    def get_pg_app_password(self) -> str:
        return "app-pwd"

    def get_pg_superuser_password(self) -> str:
        return "super-pwd"

    def get_jwt_secret(self) -> str:
        return "jwt-secret"

    def get_license_file(self) -> bytes | None:
        return None


class _NoopStorage(_SecretStorage):
    def __init__(self) -> None:
        self.insert_calls = 0

    def insert_secret(self, ref: str, kind: SecretKind, ciphertext: bytes) -> None:
        self.insert_calls += 1

    def get_secret(self, ref: str) -> _SecretRecord | None:
        raise AssertionError("not used")

    def touch_secret(self, ref: str) -> None:
        raise AssertionError("not used")

    def update_secret_ciphertext(self, ref: str, ciphertext: bytes) -> None:
        raise AssertionError("not used")

    def update_many_ciphertexts(self, ciphertext_by_ref: Mapping[str, bytes]) -> None:
        raise AssertionError("not used")

    def delete_secret(self, ref: str) -> None:
        raise AssertionError("not used")

    def list_secrets(self) -> list[_SecretRecord]:
        raise AssertionError("not used")

    def write_reveal_audit(self, ref: SecretRef, context: SecretAuditContext) -> None:
        raise AssertionError("not used")


class _FakeKind:
    def __init__(self, value: str) -> None:
        self.value = value


def _fernet_key() -> bytes:
    return base64.urlsafe_b64encode(b"test-master-key-material-0000000")
