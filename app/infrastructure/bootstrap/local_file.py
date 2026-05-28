from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from app.infrastructure.bootstrap.protocol import BootstrapSecrets


class BootstrapSecretError(RuntimeError):
    """Bootstrap Secret 读取失败。错误信息不得包含 secret 内容。"""


class BootstrapSecretPermissionError(BootstrapSecretError):
    """Bootstrap Secret 文件权限不符合契约 §7.3.1。"""


@dataclass(frozen=True)
class LocalFileBootstrapSecrets(BootstrapSecrets):
    """从本地文件系统读取 Bootstrap Secret,绝不访问 PG。"""

    master_key_file: Path = Path("config/.secret_master.key")
    pg_app_password_file: Path = Path("config/secrets/pg_app_password")
    pg_superuser_password_file: Path = Path("config/secrets/pg_superuser_password")
    license_file: Path = Path("config/license.lic")

    @classmethod
    def from_config_dir(cls, config_dir: str | Path) -> LocalFileBootstrapSecrets:
        root = Path(config_dir)
        return cls(
            master_key_file=root / ".secret_master.key",
            pg_app_password_file=root / "secrets" / "pg_app_password",
            pg_superuser_password_file=root / "secrets" / "pg_superuser_password",
            license_file=root / "license.lic",
        )

    def get_master_key(self) -> bytes:
        return self._read_required_bytes(self.master_key_file).strip()

    def get_pg_app_password(self) -> str:
        return self._read_required_text(self.pg_app_password_file)

    def get_pg_superuser_password(self) -> str:
        return self._read_required_text(self.pg_superuser_password_file)

    def get_license_file(self) -> bytes | None:
        if not self.license_file.exists():
            return None
        return self._read_required_bytes(self.license_file)

    def _read_required_text(self, path: Path) -> str:
        return self._read_required_bytes(path).decode("utf-8").strip()

    def _read_required_bytes(self, path: Path) -> bytes:
        if not path.exists():
            raise BootstrapSecretError(f"Bootstrap secret file does not exist: {path}")
        if not path.is_file():
            raise BootstrapSecretError(f"Bootstrap secret path is not a file: {path}")
        self._ensure_secure_permissions(path)
        return path.read_bytes()

    def _ensure_secure_permissions(self, path: Path) -> None:
        if _is_windows():
            # TODO(2.0.x): enforce Windows ACLs for portable installs.
            return

        mode = stat.S_IMODE(path.stat().st_mode)
        expected = stat.S_IRUSR
        if mode != expected:
            raise BootstrapSecretPermissionError(
                f"Bootstrap secret file must be 0400 on Unix-like platforms: {path}"
            )


def _is_windows() -> bool:
    return os.name == "nt" or sys.platform.startswith("win")
