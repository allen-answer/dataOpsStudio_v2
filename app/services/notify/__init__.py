"""Workflow 运行通知渠道层(C-9 设计稿 PR2)。

- ``NotifyChannel`` 抽象 + ``WebhookChannel`` / ``WecomChannel``(纯出站 HTTP,stdlib urllib)。
- ``WorkflowNotifyService`` —— 逐 target 失败隔离,从不向调用方抛(C-9 硬红线)。

R1:本包只做出站 HTTP,不 import 数据库驱动;SecretStore 经 reveal 回调依赖注入。
R2/R5:见各模块 docstring。
"""

from __future__ import annotations

from app.services.notify.channels import (
    NotifyChannel,
    RevealSecret,
    WebhookChannel,
    WecomChannel,
    default_channels,
)
from app.services.notify.service import WorkflowNotifyService

__all__ = [
    "NotifyChannel",
    "RevealSecret",
    "WebhookChannel",
    "WecomChannel",
    "WorkflowNotifyService",
    "default_channels",
]
