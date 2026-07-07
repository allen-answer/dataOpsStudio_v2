"""secret_refs kind: allow smtp_password (C-9 PR5 email channel)

C-9 邮件通知渠道:SMTP 密码是 Application Secret(R4 边界内,非 Bootstrap),
入 secret_refs 表。放开 kind CHECK 约束,追加 'smtp_password'。

★ 与 app.domain.secret.SecretKind / app.db.models.APPLICATION_SECRET_KINDS 严格一致。
★ 约束名用逻辑名 ``kind_is_application_secret`` —— metadata naming_convention
  (models.py:46 ``ck_%(table_name)s_%(constraint_name)s``)会自动补 ``ck_secret_refs_``
  前缀;写全名会双重前缀(同 0001_initial.py:276 的告诫)。

Revision ID: 0021_secret_refs_smtp_kind
Revises: 0020_compare_file_source
Create Date: 2026-07-07
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021_secret_refs_smtp_kind"
down_revision: str | None = "0020_compare_file_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS_WITH_SMTP = (
    "kind IN ('datasource_password', 'ai_api_key', 'mfa_totp_seed', "
    "'oauth_token', 'webhook_secret', 'signed_url_secret', 'smtp_password')"
)
_KINDS_WITHOUT_SMTP = (
    "kind IN ('datasource_password', 'ai_api_key', 'mfa_totp_seed', "
    "'oauth_token', 'webhook_secret', 'signed_url_secret')"
)


def upgrade() -> None:
    op.drop_constraint(
        "kind_is_application_secret",
        "secret_refs",
        type_="check",
    )
    op.create_check_constraint(
        "kind_is_application_secret",
        "secret_refs",
        _KINDS_WITH_SMTP,
    )


def downgrade() -> None:
    # 回滚前清掉新 kind 行,避免旧 CHECK 约束创建失败
    op.execute("DELETE FROM secret_refs WHERE kind = 'smtp_password'")
    op.drop_constraint(
        "kind_is_application_secret",
        "secret_refs",
        type_="check",
    )
    op.create_check_constraint(
        "kind_is_application_secret",
        "secret_refs",
        _KINDS_WITHOUT_SMTP,
    )
