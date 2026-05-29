from __future__ import annotations

from typing import Protocol


class BootstrapSecrets(Protocol):
    """Bootstrap Secret —— 连 PG 之前必须可用,**绝不存 PG**(契约 §3.3、设计稿 §7.3.1)。

    存于文件系统(portable / on-prem)或 KMS(hosted):
    - master key             config/.secret_master.key            0600
    - PG app user password   config/secrets/pg_app_password       0600
    - PG superuser password  config/secrets/pg_superuser_password 0600
    - JWT signing secret     config/secrets/jwt_secret            0600
    - license file           config/license.lic                   0600

    启动顺序:
      读 master key(文件)→ 读 PG 密码(文件)→ 连 PG
      → 用 master key 解密 PG 里的 Application Secret(SecretStore)
    避免"连 PG 需要密码、密码却在 PG 里"的启动循环(R4 根因)。

    与 SecretStore 一样使用 Protocol 声明接口;LocalFileBootstrapSecrets 是
    portable / on-prem 的文件系统实现。
    """

    def get_master_key(self) -> bytes: ...

    def get_pg_app_password(self) -> str: ...

    def get_pg_superuser_password(self) -> str: ...

    def get_jwt_secret(self) -> str: ...

    def get_license_file(self) -> bytes | None: ...
