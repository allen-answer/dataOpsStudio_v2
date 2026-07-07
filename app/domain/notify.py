"""Workflow 运行通知领域模型(C-9 设计稿 PR1;契约 §4 R2/R5)。

纯模型 + 构造期校验,零 IO(R1:domain 层禁止 import 数据库驱动)。

关键红线约束(见 docs/design/C-9-run-notifications.md §五):

- **R2**:webhook / 企业微信的**整条 url**(企微 token 藏在 url ``?key=`` 段)当作
  secret 存入 SecretStore,配置里**只留 ``url_secret_ref``**(SecretRef.ref 字符串),
  绝不出现明文 url / token。``NotifyTarget`` 因此没有任何明文 url 字段。
- **R5**:``NotifyResult`` 只带 ``target_id`` / ``channel``,**不带 url**;``RunNotification``
  的 ``error`` 摘要构造期截断,降低把内网连接串 / 密码风格文本外发的风险。

覆盖 webhook + 企业微信 + 邮件(SMTP)三渠道;邮件的 SMTP 密码同样只持
``password_secret_ref``(``SecretRef.ref``),**绝不存明文密码**(R2)。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# 支持的通知渠道(纯出站 HTTP 的 webhook / 企业微信 + SMTP 邮件)。
NotifyChannelName = Literal["webhook", "wecom", "email"]

# 终态事件白名单:run 终态字符串 + "all"(匹配任意终态)。
NotifyEvent = Literal["success", "failed", "timeout", "cancelled", "all"]

# 发给第三方的 error 摘要最大长度(R5 防御:截断,降低外发内网连接串/密码风格文本的面)。
ERROR_SUMMARY_MAX_LEN = 500


def _default_events() -> list[NotifyEvent]:
    """默认只订阅 failed(最小噪音,沿用 1.x 语义)。"""
    return ["failed"]


class NotifyTarget(BaseModel):
    """单个通知目标(存入 ``WorkflowSpec.notifications`` → dag_jsonb,无迁移)。

    webhook / 企微:``url_secret_ref`` 是 SecretStore 里整条 url 的引用
    (``SecretRef.ref``),★ **绝不存明文 url / token**(R2)。

    email(SMTP):连接信息(host/port/from/to/user)非敏感,明文存;SMTP 密码
    只持 ``password_secret_ref``(``SecretRef.ref``),★ **绝不存明文密码**(R2)。

    两类字段按 ``channel`` 分别必填(见 ``_validate_channel_fields``):非邮件渠道
    必须有 ``url_secret_ref`` 且不需要 email 字段;邮件渠道必须有 host/from/to。
    密钥发送前一刻经 reveal 回调解密。
    """

    model_config = ConfigDict(frozen=True)

    id: str
    channel: NotifyChannelName
    # webhook / wecom:整条 url(含 token)在 SecretStore 里,配置只留 ref
    url_secret_ref: str | None = None
    # email(SMTP)连接信息:均为非敏感元数据,明文存
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_from: str | None = None
    smtp_to: list[str] = Field(default_factory=list)
    smtp_user: str | None = None
    # SMTP 密码在 SecretStore 里(SecretKind.SMTP_PASSWORD),配置只留 ref
    password_secret_ref: str | None = None
    events: list[NotifyEvent] = Field(default_factory=_default_events)
    enabled: bool = True
    timeout_seconds: int = Field(default=5, ge=1, le=60)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("invalid_notify_target_id: 通知目标 id 不能为空")
        return value

    @field_validator("events")
    @classmethod
    def _validate_events(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("invalid_notify_events: events 不能为空列表(不订阅请禁用 enabled)")
        return value

    @model_validator(mode="after")
    def _validate_channel_fields(self) -> NotifyTarget:
        """按渠道校验必填:非邮件必须有 url_secret_ref;邮件必须有 host/from/to。"""
        if self.channel == "email":
            missing = [
                name
                for name, value in (
                    ("smtp_host", self.smtp_host),
                    ("smtp_from", self.smtp_from),
                )
                if not (value or "").strip()
            ]
            if not self.smtp_to:
                missing.append("smtp_to")
            if missing:
                raise ValueError(
                    f"invalid_email_target: email 通知目标缺少必填字段 {', '.join(missing)}"
                )
        else:  # webhook / wecom
            if not (self.url_secret_ref or "").strip():
                raise ValueError("invalid_url_secret_ref: url_secret_ref 不能为空")
        return self

    def accepts(self, run_status: str) -> bool:
        """该目标是否接收此终态事件(``all`` 或精确匹配 run 终态字符串)。"""
        return "all" in self.events or run_status in self.events


class RunNotification(BaseModel):
    """发给渠道的 workflow run 终态消息载荷(1.x ``_workflow_run_payload`` 的 2.0 适配)。

    字段均为非敏感元数据;``error`` 构造期截断(R5)。webhook 直接发本模型的
    ``model_dump(mode="json")``,企微渲染成 markdown。
    """

    model_config = ConfigDict(frozen=True)

    workflow_id: str
    workflow_name: str | None = None
    project: str | None = None
    run_job_id: str
    trigger: str | None = None
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    elapsed_seconds: float | None = None
    error: str | None = None
    node_status_counts: dict[str, int] = Field(default_factory=dict)

    @field_validator("error")
    @classmethod
    def _truncate_error(cls, value: str | None) -> str | None:
        if value is not None and len(value) > ERROR_SUMMARY_MAX_LEN:
            return value[:ERROR_SUMMARY_MAX_LEN] + "…(truncated)"
        return value


class NotifyResult(BaseModel):
    """单个目标的发送结果(仅观测,调用方不 persist)。

    ★ R5:只带 ``target_id`` / ``channel``,**绝不带 url**;``error`` 是分类字符串
    (如 ``http_error status=500``),不含 raw url / token。
    """

    model_config = ConfigDict(frozen=True)

    channel: str
    target_id: str
    ok: bool
    error: str | None = None
