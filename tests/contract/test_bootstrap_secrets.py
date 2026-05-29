"""BootstrapSecrets 契约测试(契约 §3.3)。

★ 注意:BootstrapSecrets 是 plain class(非 Protocol),与 SecretStore 风格
不一致(契约抽写差异)。Step 1.3 已按契约字面实现,此契约测试同样按 class
基类模式。

Codex T3 实现 LocalFile / EnvFile / Kms variants 后:
1. 实现 bootstrap fixture(各形态参数化)
2. 删除 @pytest.mark.skip
"""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path

import pytest

from app.infrastructure.bootstrap.local_file import (
    BootstrapSecretPermissionError,
    LocalFileBootstrapSecrets,
)

pytestmark = pytest.mark.contract


@pytest.fixture
def bootstrap(tmp_path: Path) -> LocalFileBootstrapSecrets:
    config_dir = tmp_path / "config"
    _write_secret(config_dir / ".secret_master.key", _fernet_key())
    _write_secret(config_dir / "secrets" / "pg_app_password", b"app-pwd\n")
    _write_secret(config_dir / "secrets" / "pg_superuser_password", b"super-pwd\n")
    _write_secret(config_dir / "secrets" / "jwt_secret", b"jwt-secret\n")
    return LocalFileBootstrapSecrets.from_config_dir(config_dir)


def test_get_master_key_returns_bytes(bootstrap: LocalFileBootstrapSecrets) -> None:
    """master key 是 bytes,不是 str(Fernet 用 bytes)。"""
    key = bootstrap.get_master_key()
    assert isinstance(key, bytes)
    assert len(key) > 0


def test_get_pg_app_password_returns_str(bootstrap: LocalFileBootstrapSecrets) -> None:
    pwd = bootstrap.get_pg_app_password()
    assert isinstance(pwd, str)
    assert len(pwd) > 0


def test_get_jwt_secret_returns_str(bootstrap: LocalFileBootstrapSecrets) -> None:
    secret = bootstrap.get_jwt_secret()
    assert isinstance(secret, str)
    assert len(secret) > 0


def test_get_license_file_optional(bootstrap: LocalFileBootstrapSecrets) -> None:
    """license 文件可能不存在(trial 模式);返回 None 而非异常。"""
    result = bootstrap.get_license_file()
    assert result is None or isinstance(result, bytes)


def test_files_are_0600_permissions(bootstrap: LocalFileBootstrapSecrets) -> None:
    """★ 契约 §7.3.1 + R8:bootstrap secret 文件必须 0600 权限。
    LocalFileBootstrapSecrets 实现需校验权限,不符抛异常。
    """
    if os.name == "nt":
        assert bootstrap.get_master_key()
        return

    mode = stat.S_IMODE(bootstrap.master_key_file.stat().st_mode)
    assert mode == stat.S_IRUSR | stat.S_IWUSR


def test_unix_rejects_non_0600_permissions(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Windows portable skips Unix permission bits; ACL enforcement is post-2.0.0")

    config_dir = tmp_path / "config"
    _write_secret(config_dir / ".secret_master.key", _fernet_key(), mode=0o400)
    _write_secret(config_dir / "secrets" / "pg_app_password", b"app-pwd\n")
    _write_secret(config_dir / "secrets" / "pg_superuser_password", b"super-pwd\n")
    _write_secret(config_dir / "secrets" / "jwt_secret", b"jwt-secret\n")

    with pytest.raises(BootstrapSecretPermissionError):
        LocalFileBootstrapSecrets.from_config_dir(config_dir).get_master_key()


def _write_secret(path: Path, value: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    if os.name != "nt":
        path.chmod(mode)


def _fernet_key() -> bytes:
    return base64.urlsafe_b64encode(b"test-master-key-material-0000000")
