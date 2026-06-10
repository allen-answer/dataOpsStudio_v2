"""AiGateway 异常(契约 §3.5、设计稿 §2.7.2/§2.7.5)。

壳级实现的最小异常集。egress / budget 拒绝必须是显式异常,
调用方据此降级(设计稿 §2.7.2 第 7 条:LLM 不可用业务功能仍可用)。
"""

from __future__ import annotations

from app.domain.ai import EgressLevel


class AiGatewayError(Exception):
    """AiGateway 相关错误基类。"""


class AiDisabledError(AiGatewayError):
    """AI 未启用(portable / on-prem 默认关,设计稿 §2.7.2 第 6 条)。"""


class EgressBlockedError(AiGatewayError):
    """内容超出允许出站等级,被 egress 钩子拦截(设计稿 §2.7.5)。

    ★ L5 在任何形态、任何配置永久禁止;L4 默认禁,需显式 opt-in;
    L2/L3 受 max_auto_egress_level 约束。
    """

    def __init__(self, level: EgressLevel, max_auto_level: EgressLevel) -> None:
        self.level = level
        self.max_auto_level = max_auto_level
        super().__init__(
            f"egress blocked: item level {level.name} exceeds allowed "
            f"max_auto level {max_auto_level.name}"
        )


class BudgetExceededError(AiGatewayError):
    """budget(token / cost)超限,在发请求前拒绝(设计稿 §2.7.2 第 4 条)。"""


class ProviderError(AiGatewayError):
    """provider 调用失败(网络 / HTTP / 解析)。

    ★ 是 AiGatewayError 子类,调用方按"AI 不可用"降级即可,不需区分网络细节。
    """
