from __future__ import annotations

import secrets as token_secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, TypeVar, runtime_checkable

import bcrypt
import structlog
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.engine import Connection, Engine, RowMapping

from app.db.models import audit_logs, secret_refs
from app.domain.secret import HashedRef, RotationReport, SecretKind, SecretRef
from app.infrastructure.bootstrap.protocol import BootstrapSecrets
from app.infrastructure.secretstore.protocol import SecretStore

logger = structlog.get_logger(__name__)

_APPLICATION_SECRET_VALUES = frozenset(kind.value for kind in SecretKind)
_FORBIDDEN_BOOTSTRAP_KINDS = frozenset(
    {
        "master_key",
        "pg_app_password",
        "pg_app_user_password",
        "pg_superuser_password",
        "license",
        "license_file",
    }
)
_T = TypeVar("_T")


class SecretStoreError(RuntimeError):
    """SecretStore 基础异常。错误信息不得包含明文 secret。"""


class SecretKindRejectedError(SecretStoreError, ValueError):
    """R4:Bootstrap Secret 不能进入 Application SecretStore。"""


class SecretAuditError(SecretStoreError):
    """reveal_secret 审计失败;默认 fail-close,不返回明文。"""


class SecretNotFoundError(SecretStoreError):
    """SecretRef 在 secret_refs 中不存在。"""


class SecretKindMismatchError(SecretStoreError):
    """调用方提供的 SecretRef.kind 与持久化 kind 不一致。"""


class SecretDecryptError(SecretStoreError):
    """ciphertext 无法用当前 master key 解密。"""


@dataclass(frozen=True)
class SecretAuditContext:
    """reveal_secret 审计上下文。

    Protocol 的 reveal_secret 签名已冻结,调用方可通过 provider 注入当前请求上下文。
    """

    purpose: str = "reveal_secret"
    user_id: str | None = None
    project_id: str | None = None
    request_id: str | None = None
    ip: str | None = None
    user_agent: str | None = None


@dataclass(frozen=True)
class _SecretRecord:
    ref: str
    kind: SecretKind
    ciphertext: bytes


@runtime_checkable
class _SecretStorage(Protocol):
    def insert_secret(self, ref: str, kind: SecretKind, ciphertext: bytes) -> None: ...

    def get_secret(self, ref: str) -> _SecretRecord | None: ...

    def touch_secret(self, ref: str) -> None: ...

    def update_secret_ciphertext(self, ref: str, ciphertext: bytes) -> None: ...

    def update_many_ciphertexts(self, ciphertext_by_ref: Mapping[str, bytes]) -> None: ...

    def delete_secret(self, ref: str) -> None: ...

    def list_secrets(self) -> list[_SecretRecord]: ...

    def write_reveal_audit(self, ref: SecretRef, context: SecretAuditContext) -> None: ...


class _SqlAlchemySecretStorage:
    """SQLAlchemy Core storage for PG secret_refs/audit_logs tables."""

    def __init__(self, bind: Engine | Connection) -> None:
        self._bind = bind

    def insert_secret(self, ref: str, kind: SecretKind, ciphertext: bytes) -> None:
        def op(conn: Connection) -> None:
            conn.execute(
                insert(secret_refs).values(ref=ref, kind=kind.value, ciphertext=ciphertext)
            )

        self._write(op)

    def get_secret(self, ref: str) -> _SecretRecord | None:
        def op(conn: Connection) -> _SecretRecord | None:
            row = (
                conn.execute(
                    select(secret_refs.c.ref, secret_refs.c.kind, secret_refs.c.ciphertext).where(
                        secret_refs.c.ref == ref
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            return _record_from_mapping(row)

        return self._read(op)

    def touch_secret(self, ref: str) -> None:
        def op(conn: Connection) -> None:
            conn.execute(
                update(secret_refs)
                .where(secret_refs.c.ref == ref)
                .values(last_accessed_at=func.now())
            )

        self._write(op)

    def update_secret_ciphertext(self, ref: str, ciphertext: bytes) -> None:
        def op(conn: Connection) -> None:
            conn.execute(
                update(secret_refs)
                .where(secret_refs.c.ref == ref)
                .values(ciphertext=ciphertext, rotation_required=False)
            )

        self._write(op)

    def update_many_ciphertexts(self, ciphertext_by_ref: Mapping[str, bytes]) -> None:
        def op(conn: Connection) -> None:
            for ref, ciphertext in ciphertext_by_ref.items():
                conn.execute(
                    update(secret_refs)
                    .where(secret_refs.c.ref == ref)
                    .values(ciphertext=ciphertext, rotation_required=False)
                )

        self._write(op)

    def delete_secret(self, ref: str) -> None:
        def op(conn: Connection) -> None:
            conn.execute(delete(secret_refs).where(secret_refs.c.ref == ref))

        self._write(op)

    def list_secrets(self) -> list[_SecretRecord]:
        def op(conn: Connection) -> list[_SecretRecord]:
            rows = (
                conn.execute(
                    select(secret_refs.c.ref, secret_refs.c.kind, secret_refs.c.ciphertext)
                )
                .mappings()
                .all()
            )
            return [_record_from_mapping(row) for row in rows]

        return self._read(op)

    def write_reveal_audit(self, ref: SecretRef, context: SecretAuditContext) -> None:
        detail: dict[str, str] = {
            "secret_ref": ref.ref,
            "kind": ref.kind.value,
            "purpose": context.purpose,
        }
        if context.user_id is not None:
            detail["user_id"] = context.user_id

        def op(conn: Connection) -> None:
            conn.execute(
                insert(audit_logs).values(
                    user_id=context.user_id,
                    project_id=context.project_id,
                    action="secret_reveal",
                    resource_type="secret_ref",
                    resource_id=ref.ref,
                    result="success",
                    request_id=context.request_id,
                    ip=context.ip,
                    user_agent=context.user_agent,
                    detail=detail,
                )
            )

        self._write(op)

    def _read(self, op: Callable[[Connection], _T]) -> _T:
        if isinstance(self._bind, Engine):
            with self._bind.connect() as conn:
                return op(conn)
        return op(self._bind)

    def _write(self, op: Callable[[Connection], _T]) -> _T:
        if isinstance(self._bind, Engine):
            with self._bind.begin() as conn:
                return op(conn)
        if self._bind.in_transaction():
            return op(self._bind)
        with self._bind.begin():
            return op(self._bind)


class LocalFileSecretStore(SecretStore):
    """Application SecretStore:master key 来自 BootstrapSecrets,secret 存 PG。"""

    def __init__(
        self,
        bind_or_storage: Engine | Connection | _SecretStorage,
        bootstrap_secrets: BootstrapSecrets,
        *,
        audit_context_provider: Callable[[], SecretAuditContext] | None = None,
        allow_audit_failure: bool = False,
    ) -> None:
        self._storage: _SecretStorage
        if isinstance(bind_or_storage, _SecretStorage):
            self._storage = bind_or_storage
        else:
            self._storage = _SqlAlchemySecretStorage(bind_or_storage)
        self._audit_context_provider = audit_context_provider or SecretAuditContext
        self._allow_audit_failure = allow_audit_failure
        self._master_key = bootstrap_secrets.get_master_key()
        self._fernet = Fernet(self._master_key)

    def store_secret(self, plaintext: str, kind: SecretKind) -> SecretRef:
        checked_kind = _validate_secret_kind(kind)
        ref_value = token_secrets.token_urlsafe(48)
        ciphertext = self._fernet.encrypt(plaintext.encode("utf-8"))
        self._storage.insert_secret(ref_value, checked_kind, ciphertext)
        return SecretRef(ref=ref_value, kind=checked_kind)

    def reveal_secret(self, ref: SecretRef) -> str:
        record = self._get_checked_record(ref)
        try:
            plaintext = self._fernet.decrypt(record.ciphertext).decode("utf-8")
        except InvalidToken as exc:
            raise SecretDecryptError(f"SecretRef cannot be decrypted: {ref.ref}") from exc

        self._audit_reveal_or_fail(ref)
        self._storage.touch_secret(ref.ref)
        return plaintext

    def rotate_secret(self, ref: SecretRef, new_plaintext: str) -> SecretRef:
        record = self._get_checked_record(ref)
        ciphertext = self._fernet.encrypt(new_plaintext.encode("utf-8"))
        self._storage.update_secret_ciphertext(record.ref, ciphertext)
        return ref

    def delete_secret(self, ref: SecretRef) -> None:
        _validate_secret_kind(ref.kind)
        self._storage.delete_secret(ref.ref)

    def hash_password(self, plaintext: str) -> HashedRef:
        hashed = bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt())
        return HashedRef(ref=hashed.decode("ascii"), algorithm="bcrypt")

    def verify_password(self, plaintext: str, ref: HashedRef) -> bool:
        if ref.algorithm.lower() != "bcrypt":
            return False
        try:
            return bcrypt.checkpw(plaintext.encode("utf-8"), ref.ref.encode("ascii"))
        except ValueError:
            return False

    def rotate_master_key(self, new_key: bytes) -> RotationReport:
        new_fernet = Fernet(new_key)
        records = self._storage.list_secrets()
        plaintext_by_ref: dict[str, bytes] = {}
        failed_refs: list[str] = []

        for record in records:
            try:
                plaintext_by_ref[record.ref] = self._fernet.decrypt(record.ciphertext)
            except InvalidToken:
                failed_refs.append(record.ref)

        if failed_refs:
            return RotationReport(rotated_count=0, failed_refs=failed_refs)

        ciphertext_by_ref = {
            ref: new_fernet.encrypt(plaintext) for ref, plaintext in plaintext_by_ref.items()
        }
        self._storage.update_many_ciphertexts(ciphertext_by_ref)
        self._master_key = new_key
        self._fernet = new_fernet
        return RotationReport(rotated_count=len(ciphertext_by_ref), failed_refs=[])

    def _get_checked_record(self, ref: SecretRef) -> _SecretRecord:
        checked_kind = _validate_secret_kind(ref.kind)
        record = self._storage.get_secret(ref.ref)
        if record is None:
            raise SecretNotFoundError(f"SecretRef not found: {ref.ref}")
        if record.kind != checked_kind:
            raise SecretKindMismatchError(f"SecretRef kind mismatch: {ref.ref}")
        return record

    def _audit_reveal_or_fail(self, ref: SecretRef) -> None:
        try:
            context = self._audit_context_provider()
            self._storage.write_reveal_audit(ref, context)
        except Exception as exc:
            if self._allow_audit_failure:
                logger.critical(
                    "secret reveal audit failed; break-glass returning plaintext",
                    secret_ref=ref.ref,
                    kind=ref.kind.value,
                    allow_audit_failure=True,
                    exc_info=True,
                )
                return
            raise SecretAuditError(f"Secret reveal audit failed: {ref.ref}") from exc


def _validate_secret_kind(kind: object) -> SecretKind:
    raw_value = _raw_kind_value(kind)
    normalized = raw_value.strip().lower()
    if normalized in _FORBIDDEN_BOOTSTRAP_KINDS or (
        normalized.startswith("pg_") and normalized.endswith("_password")
    ):
        raise SecretKindRejectedError(
            f"Bootstrap secret kind is not allowed in secret_refs: {raw_value}"
        )

    if not isinstance(kind, SecretKind):
        raise TypeError("kind must be an app.domain.secret.SecretKind instance")

    if kind.value not in _APPLICATION_SECRET_VALUES:
        raise SecretKindRejectedError(
            f"Secret kind is not an Application Secret kind: {kind.value}"
        )
    return kind


def _raw_kind_value(kind: object) -> str:
    value = getattr(kind, "value", kind)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _record_from_mapping(row: RowMapping) -> _SecretRecord:
    kind_value = str(row["kind"])
    try:
        kind = SecretKind(kind_value)
    except ValueError as exc:
        raise SecretStoreError(f"Stored secret kind is invalid: {kind_value}") from exc
    return _SecretRecord(
        ref=str(row["ref"]),
        kind=kind,
        ciphertext=_coerce_bytes(row["ciphertext"]),
    )


def _coerce_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    raise SecretStoreError("Stored ciphertext is not bytes")
