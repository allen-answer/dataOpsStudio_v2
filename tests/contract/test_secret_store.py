"""SecretStore Protocol 契约测试(契约 §3.3)。

Codex T3 实现 SecretStore 实现类后:
1. 实现 secret_store fixture
2. 删除 @pytest.mark.skip
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Mapping
from typing import Any, cast

import pytest
import structlog

from app.domain.secret import RotationReport, SecretKind, SecretRef
from app.infrastructure.bootstrap.protocol import BootstrapSecrets
from app.infrastructure.secretstore.local_file import (
    LocalFileSecretStore,
    SecretAuditContext,
    SecretAuditError,
    SecretKindRejectedError,
    SecretNotFoundError,
    _SecretRecord,
    _SecretStorage,
)
from app.infrastructure.secretstore.protocol import SecretStore

pytestmark = pytest.mark.contract


@pytest.fixture
def secret_store() -> SecretStore:
    """SecretStore impl backed by in-memory storage for protocol behavior tests."""
    return _build_secret_store()


def test_store_then_reveal_roundtrip(secret_store: SecretStore) -> None:
    ref = secret_store.store_secret("hunter2", SecretKind.DATASOURCE_PASSWORD)
    assert ref.kind == SecretKind.DATASOURCE_PASSWORD
    assert secret_store.reveal_secret(ref) == "hunter2"


def test_r4_store_secret_rejects_bootstrap_kinds(secret_store: SecretStore) -> None:
    """★ R4 红线 Python 层防御:store_secret 不接受 PG_* / MASTER_KEY 等 kind。

    SecretKind enum 本身已排除 Bootstrap kinds,但实现仍需在 store_secret
    入口处 fail-fast(防 cast / 反射构造的 SecretKind 字符串绕过)。
    """
    bad_kinds: list[Any] = [
        "PG_APP_USER_PASSWORD",
        "PG_SUPERUSER_PASSWORD",
        "pg_app_password",
        "master_key",
        _FakeKind("pg_superuser_password"),
    ]
    for bad_kind in bad_kinds:
        with pytest.raises(SecretKindRejectedError):
            secret_store.store_secret("must-not-store", cast(SecretKind, bad_kind))


def test_rotate_secret_changes_ciphertext_keep_plain(secret_store: SecretStore) -> None:
    ref = secret_store.store_secret("old_pwd", SecretKind.DATASOURCE_PASSWORD)
    new_ref = secret_store.rotate_secret(ref, "new_pwd")
    assert secret_store.reveal_secret(new_ref) == "new_pwd"


def test_delete_secret_makes_reveal_fail(secret_store: SecretStore) -> None:
    ref = secret_store.store_secret("pwd", SecretKind.DATASOURCE_PASSWORD)
    secret_store.delete_secret(ref)
    with pytest.raises(SecretNotFoundError):
        secret_store.reveal_secret(ref)


def test_hash_and_verify_password_roundtrip(secret_store: SecretStore) -> None:
    hashed = secret_store.hash_password("hunter2")
    assert hashed.algorithm == "bcrypt"
    assert secret_store.verify_password("hunter2", hashed) is True
    assert secret_store.verify_password("wrong", hashed) is False


def test_reveal_secret_is_audited() -> None:
    """★ 契约 §7.7:reveal_secret 每次调用必落 audit_logs。
    Codex 实现需注入 AuditLogger 并验证记录写入。
    """
    storage = _InMemorySecretStorage()
    store = _build_secret_store(
        storage,
        audit_context=SecretAuditContext(
            purpose="datasource_connect",
            user_id="user-1",
            project_id="project-1",
            request_id="req-1",
        ),
    )

    ref = store.store_secret("hunter2", SecretKind.DATASOURCE_PASSWORD)
    assert store.reveal_secret(ref) == "hunter2"

    assert storage.audit_details == [
        {
            "secret_ref": ref.ref,
            "kind": "datasource_password",
            "purpose": "datasource_connect",
            "user_id": "user-1",
        }
    ]
    assert "hunter2" not in repr(storage.audit_details)


def test_reveal_secret_fails_closed_when_audit_write_fails() -> None:
    storage = _InMemorySecretStorage()
    storage.fail_audit = True
    store = _build_secret_store(storage)

    ref = store.store_secret("hunter2", SecretKind.DATASOURCE_PASSWORD)
    with pytest.raises(SecretAuditError):
        store.reveal_secret(ref)


def test_reveal_secret_break_glass_returns_plaintext_and_logs_critical(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _configure_structlog_for_caplog()
    caplog.set_level(logging.CRITICAL)
    storage = _InMemorySecretStorage()
    storage.fail_audit = True
    store = _build_secret_store(storage, allow_audit_failure=True)

    ref = store.store_secret("hunter2", SecretKind.DATASOURCE_PASSWORD)

    assert store.reveal_secret(ref) == "hunter2"
    assert any(
        record.levelno == logging.CRITICAL
        and "break-glass returning plaintext" in record.getMessage()
        for record in caplog.records
    )


def test_rotate_master_key_re_encrypts_all() -> None:
    """master key 轮换:旧 key 解密 → 新 key 重加密 → 写回。
    Bootstrap secret(PG 密码)单独轮换,不在此 store 范围。
    """
    storage = _InMemorySecretStorage()
    store = _build_secret_store(storage)
    ref = store.store_secret("hunter2", SecretKind.DATASOURCE_PASSWORD)
    old_ciphertext = storage.records[ref.ref].ciphertext

    report = store.rotate_master_key(_rotated_fernet_key())

    assert report == RotationReport(rotated_count=1, failed_refs=[])
    assert storage.records[ref.ref].ciphertext != old_ciphertext
    assert store.reveal_secret(ref) == "hunter2"


def _build_secret_store(
    storage: _InMemorySecretStorage | None = None,
    *,
    audit_context: SecretAuditContext | None = None,
    allow_audit_failure: bool = False,
) -> LocalFileSecretStore:
    actual_storage = storage or _InMemorySecretStorage()
    actual_context = audit_context or SecretAuditContext()
    return LocalFileSecretStore(
        actual_storage,
        _StaticBootstrapSecrets(_fernet_key()),
        audit_context_provider=lambda: actual_context,
        allow_audit_failure=allow_audit_failure,
    )


class _StaticBootstrapSecrets(BootstrapSecrets):
    def __init__(self, master_key: bytes) -> None:
        self._master_key = master_key

    def get_master_key(self) -> bytes:
        return self._master_key

    def get_pg_app_password(self) -> str:
        return "app-pwd"

    def get_pg_superuser_password(self) -> str:
        return "super-pwd"

    def get_license_file(self) -> bytes | None:
        return None


class _InMemorySecretStorage(_SecretStorage):
    def __init__(self) -> None:
        self.records: dict[str, _SecretRecord] = {}
        self.audit_details: list[dict[str, str]] = []
        self.fail_audit = False

    def insert_secret(self, ref: str, kind: SecretKind, ciphertext: bytes) -> None:
        self.records[ref] = _SecretRecord(ref=ref, kind=kind, ciphertext=ciphertext)

    def get_secret(self, ref: str) -> _SecretRecord | None:
        return self.records.get(ref)

    def touch_secret(self, ref: str) -> None:
        if ref not in self.records:
            raise SecretNotFoundError(f"SecretRef not found: {ref}")

    def update_secret_ciphertext(self, ref: str, ciphertext: bytes) -> None:
        record = self.records[ref]
        self.records[ref] = _SecretRecord(ref=ref, kind=record.kind, ciphertext=ciphertext)

    def update_many_ciphertexts(self, ciphertext_by_ref: Mapping[str, bytes]) -> None:
        for ref, ciphertext in ciphertext_by_ref.items():
            self.update_secret_ciphertext(ref, ciphertext)

    def delete_secret(self, ref: str) -> None:
        self.records.pop(ref, None)

    def list_secrets(self) -> list[_SecretRecord]:
        return list(self.records.values())

    def write_reveal_audit(self, ref: SecretRef, context: SecretAuditContext) -> None:
        if self.fail_audit:
            raise RuntimeError("audit sink failed")
        detail = {
            "secret_ref": ref.ref,
            "kind": ref.kind.value,
            "purpose": context.purpose,
        }
        if context.user_id is not None:
            detail["user_id"] = context.user_id
        self.audit_details.append(detail)


class _FakeKind:
    def __init__(self, value: str) -> None:
        self.value = value


def _fernet_key() -> bytes:
    return base64.urlsafe_b64encode(b"test-master-key-material-0000000")


def _rotated_fernet_key() -> bytes:
    return base64.urlsafe_b64encode(b"rotated-master-key-material-0000")


def _configure_structlog_for_caplog() -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.EventRenamer("message"),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
