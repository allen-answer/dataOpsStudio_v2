"""SecretStore Protocol 契约测试(契约 §3.3)。

Codex T3 实现 SecretStore 实现类后:
1. 实现 secret_store fixture
2. 删除 @pytest.mark.skip
"""

from __future__ import annotations

import pytest

from app.domain.secret import SecretKind
from app.infrastructure.secretstore.protocol import SecretStore

pytestmark = pytest.mark.contract


@pytest.fixture
def secret_store() -> SecretStore:
    """SecretStore impl —— Codex T3 后改成返回真实实例。"""
    pytest.skip("Codex T3(infrastructure/secretstore/)实现后启用")


@pytest.mark.skip(reason="Codex T3 implements SecretStore")
def test_store_then_reveal_roundtrip(secret_store: SecretStore) -> None:
    ref = secret_store.store_secret("hunter2", SecretKind.DATASOURCE_PASSWORD)
    assert ref.kind == SecretKind.DATASOURCE_PASSWORD
    assert secret_store.reveal_secret(ref) == "hunter2"


@pytest.mark.skip(reason="Codex T3 implements SecretStore (R4 红线 enforcement)")
def test_r4_store_secret_rejects_bootstrap_kinds(secret_store: SecretStore) -> None:
    """★ R4 红线 Python 层防御:store_secret 不接受 PG_* / MASTER_KEY 等 kind。

    SecretKind enum 本身已排除 Bootstrap kinds,但实现仍需在 store_secret
    入口处 fail-fast(防 cast / 反射构造的 SecretKind 字符串绕过)。
    """
    # 这里需要构造一个"非法 kind"的场景。SecretKind enum 限制了合法值,
    # 实现还要拒绝任何尝试绕过 enum(如果用字符串接口)。Codex 决定具体实现。
    pass


@pytest.mark.skip(reason="Codex T3 implements SecretStore")
def test_rotate_secret_changes_ciphertext_keep_plain(secret_store: SecretStore) -> None:
    ref = secret_store.store_secret("old_pwd", SecretKind.DATASOURCE_PASSWORD)
    new_ref = secret_store.rotate_secret(ref, "new_pwd")
    assert secret_store.reveal_secret(new_ref) == "new_pwd"


@pytest.mark.skip(reason="Codex T3 implements SecretStore")
def test_delete_secret_makes_reveal_fail(secret_store: SecretStore) -> None:
    ref = secret_store.store_secret("pwd", SecretKind.DATASOURCE_PASSWORD)
    secret_store.delete_secret(ref)
    with pytest.raises(Exception):  # noqa: B017 — 具体异常类型由 Codex impl 决定
        secret_store.reveal_secret(ref)


@pytest.mark.skip(reason="Codex T3 implements SecretStore")
def test_hash_and_verify_password_roundtrip(secret_store: SecretStore) -> None:
    hashed = secret_store.hash_password("hunter2")
    assert hashed.algorithm == "bcrypt"
    assert secret_store.verify_password("hunter2", hashed) is True
    assert secret_store.verify_password("wrong", hashed) is False


@pytest.mark.skip(reason="Codex T3 implements SecretStore (R5 配合:reveal 必审计)")
def test_reveal_secret_is_audited(secret_store: SecretStore) -> None:
    """★ 契约 §7.7:reveal_secret 每次调用必落 audit_logs。
    Codex 实现需注入 AuditLogger 并验证记录写入。
    """
    pass


@pytest.mark.skip(reason="Codex T3 implements SecretStore (master key 轮换)")
def test_rotate_master_key_re_encrypts_all(secret_store: SecretStore) -> None:
    """master key 轮换:旧 key 解密 → 新 key 重加密 → 写回。
    Bootstrap secret(PG 密码)单独轮换,不在此 store 范围。
    """
    pass
