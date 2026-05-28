from __future__ import annotations

from typing import Protocol

from app.domain.secret import HashedRef, RotationReport, SecretKind, SecretRef


class SecretStore(Protocol):
    """Application Secret 管理(契约 §3.3、设计稿 §7.4)。

    存于 PG secret_refs 表,master key(Fernet)加密。仅 Application Secret —
    PG 连接密码、master key、license 等 Bootstrap Secret 走 BootstrapSecrets,
    **绝不进 secret_refs 表**(R4 红线,避免启动循环)。

    实现形态:
    - hosted:KmsSecretStore(腾讯云 KMS 等)
    - on-prem Docker:EnvFileSecretStore
    - Windows Portable:LocalFileSecretStore

    R3 红线:Fernet / bcrypt import 只允许在 app/infrastructure/secretstore/
    (本目录已豁免 TID251)。业务代码不得直接 import 这些原语。

    审计:reveal_secret 每次调用必落 audit_logs(契约 §7.7)。
    """

    def store_secret(self, plaintext: str, kind: SecretKind) -> SecretRef:
        """加密 + 落 secret_refs;★ kind 不接受 PG_*_PASSWORD / MASTER_KEY(R4)。"""
        raise NotImplementedError

    def reveal_secret(self, ref: SecretRef) -> str:
        """解密取明文。★ 每次调用必审计(谁/为何/何时)。"""
        raise NotImplementedError

    def rotate_secret(self, ref: SecretRef, new_plaintext: str) -> SecretRef:
        raise NotImplementedError

    def delete_secret(self, ref: SecretRef) -> None:
        raise NotImplementedError

    def hash_password(self, plaintext: str) -> HashedRef:
        """bcrypt 单向 hash,用于密码验证(不可逆)。"""
        raise NotImplementedError

    def verify_password(self, plaintext: str, ref: HashedRef) -> bool:
        raise NotImplementedError

    def rotate_master_key(self, new_key: bytes) -> RotationReport:
        """旧 key 解密所有 Application Secret → 新 key 重加密 → 写回。
        Bootstrap Secret(PG 密码等)由 BootstrapSecrets 单独轮换,不在此处理。
        """
        raise NotImplementedError
