"""Workflow 运行通知服务(逐 target 失败隔离,从不向调用方抛)。

移植 1.x ``notifier.notify_workflow_run`` 的语义:逐 target 发送,每个 target 的
异常被捕获落 ``NotifyResult(ok=False)``,**从不向 workflow 执行路径抛**(C-9 硬红线)。

本 PR 不接 worker;worker 侧终态钩子(``_notify_workflow_run_terminal``)属后续 PR。
"""

from __future__ import annotations

from collections.abc import Mapping

import structlog

from app.domain.notify import NotifyResult, RunNotification
from app.domain.workflow import WorkflowSpec
from app.services.notify.channels import NotifyChannel, RevealSecret, default_channels

_log = structlog.get_logger(__name__)


class WorkflowNotifyService:
    """按 ``WorkflowSpec.notifications`` 分发 run 终态通知。

    ``reveal``:``SecretStore.reveal_secret``(DI;worker 侧后续 PR 传入其 store 实例)。
    ``channels``:channel 名 → 实现;缺省用 ``default_channels()``(webhook + wecom)。
    """

    def __init__(
        self,
        reveal: RevealSecret,
        channels: Mapping[str, NotifyChannel] | None = None,
    ) -> None:
        self._reveal = reveal
        self._channels = channels if channels is not None else default_channels()

    def notify(
        self, spec: WorkflowSpec, payload: RunNotification, run_status: str
    ) -> list[NotifyResult]:
        """逐 target 发送匹配 ``run_status`` 的通知,返回每 target 结果(仅观测)。

        ★ 失败隔离:单 target 异常不影响其它 target,整体从不抛(即便渠道实现违约抛了)。
        无匹配 target = 静默返回空列表(no-op)。
        """
        results: list[NotifyResult] = []
        for target in spec.notifications:
            if not target.enabled or not target.accepts(run_status):
                continue
            channel = self._channels.get(target.channel)
            if channel is None:
                _log.warning("notify channel unknown", channel=target.channel, target_id=target.id)
                results.append(
                    NotifyResult(
                        channel=target.channel,
                        target_id=target.id,
                        ok=False,
                        error="unknown_channel",
                    )
                )
                continue
            try:
                results.append(channel.send(target, payload, reveal=self._reveal))
            except Exception:  # 双层失败隔离:渠道理应不抛,这里再兜一层
                _log.warning("notify target failed", channel=target.channel, target_id=target.id)
                results.append(
                    NotifyResult(
                        channel=target.channel,
                        target_id=target.id,
                        ok=False,
                        error="channel_error",
                    )
                )
        return results
