"""BootstrapSecrets 契约测试(契约 §3.3)。

★ 注意:BootstrapSecrets 是 plain class(非 Protocol),与 SecretStore 风格
不一致(契约抽写差异)。Step 1.3 已按契约字面实现,此契约测试同样按 class
基类模式。

Codex T3 实现 LocalFile / EnvFile / Kms variants 后:
1. 实现 bootstrap fixture(各形态参数化)
2. 删除 @pytest.mark.skip
"""

from __future__ import annotations

import pytest

from app.infrastructure.bootstrap.protocol import BootstrapSecrets

pytestmark = pytest.mark.contract


@pytest.fixture
def bootstrap() -> BootstrapSecrets:
    pytest.skip("Codex T3(infrastructure/bootstrap/)实现后启用")


@pytest.mark.skip(reason="Codex T3 implements BootstrapSecrets")
def test_get_master_key_returns_bytes(bootstrap: BootstrapSecrets) -> None:
    """master key 是 bytes,不是 str(Fernet 用 bytes)。"""
    key = bootstrap.get_master_key()
    assert isinstance(key, bytes)
    assert len(key) > 0


@pytest.mark.skip(reason="Codex T3 implements BootstrapSecrets")
def test_get_pg_app_password_returns_str(bootstrap: BootstrapSecrets) -> None:
    pwd = bootstrap.get_pg_app_password()
    assert isinstance(pwd, str)
    assert len(pwd) > 0


@pytest.mark.skip(reason="Codex T3 implements BootstrapSecrets")
def test_get_license_file_optional(bootstrap: BootstrapSecrets) -> None:
    """license 文件可能不存在(trial 模式);返回 None 而非异常。"""
    result = bootstrap.get_license_file()
    assert result is None or isinstance(result, bytes)


@pytest.mark.skip(reason="Codex T3 implements BootstrapSecrets (R8 配合)")
def test_files_are_0400_permissions(bootstrap: BootstrapSecrets) -> None:
    """★ 契约 §7.3.1 + R8:bootstrap secret 文件必须 0400 权限。
    LocalFileBootstrapSecrets 实现需校验权限,不符抛异常。
    """
    pass
