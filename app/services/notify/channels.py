"""通知渠道实现(webhook + 企业微信;纯出站 HTTP POST,stdlib urllib)。

移植 1.x ``services/notifier.py`` 的渠道逻辑 + 企微 markdown 模板 + 失败隔离,
重做密钥来源(env/JSON 明文 → ``reveal`` 回调经 SecretStore 解密)。

红线约束:

- **R1**:只用 stdlib ``urllib.request`` 做出站 HTTP,不 import 任何数据库驱动。
- **R2**:整条 url(含企微 ``?key=token``)是 secret,发送前一刻经 ``reveal`` 回调
  从 SecretStore 解密,**不落配置 / 不长留内存 / 不进日志**。
- **R5**:``send`` **绝不 log raw url**(``url``/``webhook`` 不在 structlog 脱敏片段,
  token 藏在 url value 里拦不住);异常也**不把 url 放进 ``NotifyResult.error``**——
  只记 ``http_error status=…`` / 异常类名等分类字符串。
- **失败隔离**:``send`` 吞掉一切异常,永远返回 ``NotifyResult``,**从不 raise**。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
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
    """发送前一刻解密整条 url(R2);调用点用完即弃,不缓存。"""
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


def default_channels() -> Mapping[str, NotifyChannel]:
    """首版内置渠道注册表(channel 名 → 实现)。"""
    return {WebhookChannel.channel: WebhookChannel(), WecomChannel.channel: WecomChannel()}
