from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class SecretKind(StrEnum):
    """Application Secret 类型(契约 §3.3、设计稿 §7.3.2)。

    ★ R4 红线:**Bootstrap Secret 永不进此枚举**。
    PG 连接密码、master key、license 文件归 BootstrapSecrets(文件系统),
    SecretStore 只管下列 Application Secret(PG secret_refs 表,master key 加密)。

    若有 PR 尝试在此添加 PG_*_PASSWORD / MASTER_KEY 之类 kind,review 拒绝。
    """

    DATASOURCE_PASSWORD = "datasource_password"
    AI_API_KEY = "ai_api_key"
    MFA_TOTP_SEED = "mfa_totp_seed"
    OAUTH_TOKEN = "oauth_token"
    WEBHOOK_SECRET = "webhook_secret"
    SIGNED_URL_SECRET = "signed_url_secret"


class SecretRef(BaseModel):
    """跨存储的逻辑引用 —— 不是 PG 外键!

    hosted 形态用 KmsSecretStore 时,ref 指向 KMS key id,**根本不在 PG 里**。
    所以 secret_refs.ref / datasources.password_secret_ref 等 DB 字段全部
    VARCHAR(64) 普通字符串,绝不加 FK。见 0001_initial.py 迁移注释。
    """

    model_config = ConfigDict(frozen=True)

    ref: str  # 64 字符标识;格式由 backend 决定(KMS arn / PG row id / file path)
    kind: SecretKind


class HashedRef(BaseModel):
    """单向 hash 引用,用于密码验证(无可逆明文)。

    与 SecretRef 区别:
    - HashedRef 是单向 hash,用 verify_password 比对(不可解密)
    - SecretRef 是可逆 ciphertext 引用,用 reveal_secret 取明文
    """

    model_config = ConfigDict(frozen=True)

    ref: str
    algorithm: str = "bcrypt"


class RotationReport(BaseModel):
    """master key 轮换报告(契约 §3.3 rotate_master_key 返回值)。"""

    rotated_count: int
    failed_refs: list[str] = Field(default_factory=list)
