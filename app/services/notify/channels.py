"""通知渠道实现(webhook + 企业微信 + 邮件;stdlib urllib / smtplib)。

移植 1.x ``services/notifier.py`` 的渠道逻辑 + 企微 markdown 模板 + 失败隔离,
重做密钥来源(env/JSON 明文 → ``reveal`` 回调经 SecretStore 解密)。

红线约束:

- **R1**:只用 stdlib ``urllib.request`` / ``smtplib`` 做出站 IO,不 import 任何数据库驱动。
- **R2**:整条 url(含企微 ``?key=token``)与 SMTP 密码都是 secret,发送前一刻经
  ``reveal`` 回调从 SecretStore 解密,**不落配置 / 不长留内存 / 不进日志**。
- **R5**:``send`` **绝不 log raw url / 密码**(``url``/``webhook`` 不在 structlog 脱敏
  片段,token 藏在 url value 里拦不住);异常也**绝不把 url / 密码放进 ``NotifyResult.error``**
  —— 只记 ``http_error status=…`` / ``smtp_error …`` / 异常类名等分类字符串。
- **失败隔离**:``send`` 吞掉一切异常,永远返回 ``NotifyResult``,**从不 raise**。
"""

from __future__ import annotations

import json
import smtplib
import socket
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from email.message import EmailMessage
from typing import Protocol
from urllib.request import Request

import structlog

from app.domain.notify import NotifyResult, NotifyTarget, RunNotification
from app.domain.secret import SecretKind, SecretRef

_log = structlog.get_logger(__name__)

# reveal 回调:入 SecretRef 出明文,签名与 SecretStore.reveal_secret 一致(DI)。
RevealSecret = Callable[[SecretRef], str]

_JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}


class NotifyChannel(Protocol):
    """渠道抽象:给定 target + payload,发送并返回结果(永不抛)。"""

    channel: str

    def send(
        self, target: NotifyTarget, payload: RunNotification, *, reveal: RevealSecret
    ) -> NotifyResult: ...


def _classify_error(exc: BaseException) -> str:
    """把异常收敛成**不含 url / token** 的短分类字符串(R5)。

    ★ 不使用 ``str(exc)``——urllib 的 ``HTTPError`` / ``URLError`` 消息可能内嵌
    请求 url(含 token)。只取状态码 / reason 类型名 / 异常类名。
    """
    if isinstance(exc, urllib.error.HTTPError):
        return f"http_error status={exc.code}"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, TimeoutError | socket.timeout):
            return "timeout"
        return f"url_error reason={type(reason).__name__}"
    if isinstance(exc, smtplib.SMTPResponseException):
        # ★ 只取状态码,不取 smtp_error(可能回显服务器消息 / 认证细节)
        return f"smtp_error status={exc.smtp_code}"
    if isinstance(exc, smtplib.SMTPException):
        return f"smtp_error {type(exc).__name__}"
    if isinstance(exc, TimeoutError | socket.timeout):
        return "timeout"
    return f"error {type(exc).__name__}"


def _post_json(url: str, body: bytes, *, timeout: int) -> None:
    """POST JSON,非 2xx 由 urllib 抛 HTTPError。调用方负责吞异常。

    ★ url 只作为 urlopen 入参,绝不出现在任何日志 / 返回值里(R5)。
    """
    request = Request(url, data=body, headers=_JSON_HEADERS, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        # 读掉响应体确保连接完成;内容不使用(第三方回执与我们无关)。
        response.read()


def _resolve_url(target: NotifyTarget, reveal: RevealSecret) -> str:
    """发送前一刻解密整条 url(R2);调用点用完即弃,不缓存。

    webhook / wecom 目标构造期已保证 ``url_secret_ref`` 非空;None 属配置异常,
    抛出后由 ``send`` 的失败隔离吞掉(不外泄细节)。
    """
    if target.url_secret_ref is None:
        raise ValueError("missing_url_secret_ref")
    ref = SecretRef(ref=target.url_secret_ref, kind=SecretKind.WEBHOOK_SECRET)
    return reveal(ref)


class WebhookChannel:
    """通用 webhook:POST 原始 ``RunNotification`` JSON。"""

    channel = "webhook"

    def send(
        self, target: NotifyTarget, payload: RunNotification, *, reveal: RevealSecret
    ) -> NotifyResult:
        try:
            url = _resolve_url(target, reveal)
            body = json.dumps(payload.model_dump(mode="json")).encode("utf-8")
            _post_json(url, body, timeout=target.timeout_seconds)
        except Exception as exc:  # 失败隔离:渠道永不抛
            error = _classify_error(exc)
            _log.warning(
                "notify webhook failed", channel=self.channel, target_id=target.id, reason=error
            )
            return NotifyResult(channel=self.channel, target_id=target.id, ok=False, error=error)
        return NotifyResult(channel=self.channel, target_id=target.id, ok=True)


class WecomChannel:
    """企业微信群机器人:POST 固定 markdown 模板(移植 1.x ``_send_wecom``)。"""

    channel = "wecom"

    def send(
        self, target: NotifyTarget, payload: RunNotification, *, reveal: RevealSecret
    ) -> NotifyResult:
        try:
            url = _resolve_url(target, reveal)
            body = json.dumps(_wecom_body(payload)).encode("utf-8")
            _post_json(url, body, timeout=target.timeout_seconds)
        except Exception as exc:  # 失败隔离:渠道永不抛
            error = _classify_error(exc)
            _log.warning(
                "notify wecom failed", channel=self.channel, target_id=target.id, reason=error
            )
            return NotifyResult(channel=self.channel, target_id=target.id, ok=False, error=error)
        return NotifyResult(channel=self.channel, target_id=target.id, ok=True)


def _wecom_body(payload: RunNotification) -> dict[str, object]:
    """企微 markdown 消息体(``msgtype=markdown``)。字段均非敏感。"""
    lines = [
        f"## Workflow {payload.status}: {payload.workflow_name or payload.workflow_id}",
        f"- run: {payload.run_job_id}",
    ]
    if payload.project:
        lines.append(f"- project: {payload.project}")
    if payload.trigger:
        lines.append(f"- trigger: {payload.trigger}")
    if payload.started_at is not None:
        lines.append(f"- started: {payload.started_at.isoformat()}")
    if payload.finished_at is not None:
        lines.append(f"- finished: {payload.finished_at.isoformat()}")
    if payload.elapsed_seconds is not None:
        lines.append(f"- elapsed: {payload.elapsed_seconds}s")
    if payload.node_status_counts:
        counts = ", ".join(f"{k}={v}" for k, v in sorted(payload.node_status_counts.items()))
        lines.append(f"- nodes: {counts}")
    if payload.error:
        lines.append(f"- error: {payload.error}")
    return {"msgtype": "markdown", "markdown": {"content": "\n".join(lines)}}


class EmailChannel:
    """SMTP 邮件(stdlib ``smtplib``;移植 1.x ``_send_email``)。

    ``password_secret_ref`` 存在时发送前一刻 reveal SMTP 密码(R2),仅用于
    ``smtp.login``,用完即弃;**绝不进日志 / NotifyResult**(R5)。无 user/密码时
    走匿名投递(部分内网 relay 场景)。
    """

    channel = "email"

    def send(
        self, target: NotifyTarget, payload: RunNotification, *, reveal: RevealSecret
    ) -> NotifyResult:
        try:
            password: str | None = None
            if target.password_secret_ref is not None:
                ref = SecretRef(ref=target.password_secret_ref, kind=SecretKind.SMTP_PASSWORD)
                password = reveal(ref)
            _send_smtp(target, _email_message(target, payload), password=password)
        except Exception as exc:  # 失败隔离:渠道永不抛
            error = _classify_error(exc)
            _log.warning(
                "notify email failed", channel=self.channel, target_id=target.id, reason=error
            )
            return NotifyResult(channel=self.channel, target_id=target.id, ok=False, error=error)
        return NotifyResult(channel=self.channel, target_id=target.id, ok=True)


def _email_message(target: NotifyTarget, payload: RunNotification) -> EmailMessage:
    """组装纯文本邮件(subject + 正文均为非敏感 run 元数据)。"""
    message = EmailMessage()
    name = payload.workflow_name or payload.workflow_id
    message["Subject"] = f"[DataOps] workflow {payload.status}: {name}"
    message["From"] = target.smtp_from or ""
    message["To"] = ", ".join(target.smtp_to)
    message.set_content(_email_body(payload))
    return message


def _email_body(payload: RunNotification) -> str:
    """邮件正文(与企微 markdown 同字段,纯文本行)。字段均非敏感。"""
    lines = [
        f"Workflow {payload.status}: {payload.workflow_name or payload.workflow_id}",
        f"run: {payload.run_job_id}",
    ]
    if payload.project:
        lines.append(f"project: {payload.project}")
    if payload.trigger:
        lines.append(f"trigger: {payload.trigger}")
    if payload.started_at is not None:
        lines.append(f"started: {payload.started_at.isoformat()}")
    if payload.finished_at is not None:
        lines.append(f"finished: {payload.finished_at.isoformat()}")
    if payload.elapsed_seconds is not None:
        lines.append(f"elapsed: {payload.elapsed_seconds}s")
    if payload.node_status_counts:
        counts = ", ".join(f"{k}={v}" for k, v in sorted(payload.node_status_counts.items()))
        lines.append(f"nodes: {counts}")
    if payload.error:
        lines.append(f"error: {payload.error}")
    return "\n".join(lines)


def _send_smtp(target: NotifyTarget, message: EmailMessage, *, password: str | None) -> None:
    """发一封邮件;非 2xx / 认证失败由 smtplib 抛,调用方负责吞异常。

    ★ password 只作为 ``login`` 入参,绝不出现在任何日志 / 返回值里(R5)。
    """
    host = target.smtp_host or ""
    with smtplib.SMTP(host, target.smtp_port, timeout=target.timeout_seconds) as smtp:
        smtp.ehlo()
        # 机会性 STARTTLS:服务器支持则升级到 TLS(避免明文投递凭据 / 内容)
        if smtp.has_extn("starttls"):
            smtp.starttls()
            smtp.ehlo()
        if target.smtp_user and password is not None:
            smtp.login(target.smtp_user, password)
        smtp.send_message(message)


def default_channels() -> Mapping[str, NotifyChannel]:
    """内置渠道注册表(channel 名 → 实现)。"""
    return {
        WebhookChannel.channel: WebhookChannel(),
        WecomChannel.channel: WecomChannel(),
        EmailChannel.channel: EmailChannel(),
    }
