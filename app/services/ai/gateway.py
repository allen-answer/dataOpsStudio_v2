from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from app.domain.ai import AiChunk, AiContext, AiOptions, AiResponse


class AiGateway(Protocol):
    """AI 出站统一入口(契约 §3.5、设计稿 §2.7.2)。

    2.0.0 只做壳:接 1 个 provider 调通即可。Assist/Copilot 具体功能后续版本。
    ★ 但 egress 检查 + 脱敏 + 审计的钩子位 **2.0.0 就要留好**——AI 是 2.0
    主线骨架的一部分(对应契约 §5 必做项)。

    实现入口:DefaultAiGateway(AiGateway)——壳级实现,验证 provider 可调通,
    L0-L5 egress 检查骨架到位,具体 provider client 由 Codex 在 Step 2 加。

    强制能力(设计稿 §2.7.2):
    1. Egress 检查(L0-L5,按形态默认策略;L5 永禁)
    2. 脱敏(SQL / 表名 / 列名 / 样本数据)
    3. 审计(每次调用落 ai_usage 表:user/purpose/tokens/cost/egress_level)
    4. Budget(per-user / per-project / per-tenant 上限)
    5. Provider 抽象(on-prem 可配私有 LLM)
    6. 关闭开关(on-prem / portable 默认关)
    7. 失败降级(LLM 不可用业务功能仍可用)
    """

    def complete(self, prompt: str, context: AiContext, options: AiOptions) -> AiResponse:
        raise NotImplementedError

    def stream_complete(
        self, prompt: str, context: AiContext, options: AiOptions
    ) -> Iterator[AiChunk]:
        """流式输出(SSE 用)。Iterator[AiChunk],最后一个 chunk finished=True。"""
        raise NotImplementedError
