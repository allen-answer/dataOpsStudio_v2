"""AiGateway Protocol 契约测试(契约 §3.5)。

Codex 实现 DefaultAiGateway 后:
1. 实现 gateway fixture
2. 删除 @pytest.mark.skip

★ 2.0.0 只做壳:接 1 provider 可调通,但 egress / 脱敏 / 审计 / budget 钩子位必须留好。
"""

from __future__ import annotations

import pytest

from app.domain.ai import AiContext, AiOptions, ContextItem, EgressLevel
from app.services.ai.gateway import AiGateway

pytestmark = pytest.mark.contract


@pytest.fixture
def ai_gateway() -> AiGateway:
    pytest.skip("Codex(services/ai/)实现 DefaultAiGateway 后启用")


@pytest.mark.skip(reason="Codex implements DefaultAiGateway")
def test_complete_returns_response(ai_gateway: AiGateway) -> None:
    ctx = AiContext(items=[ContextItem(content="hello", egress_level=EgressLevel.L0)])
    resp = ai_gateway.complete("Hi", ctx, AiOptions(purpose="test"))
    assert resp.content is not None


@pytest.mark.skip(reason="Codex implements DefaultAiGateway (★ L5 永禁出站)")
def test_l5_content_blocked(ai_gateway: AiGateway) -> None:
    """★ 设计稿 §2.7.5:L5(密码/token/PII)在任何形态、任何配置永禁出站。
    Gateway 检测到 L5 命中即拦截 + 审计;不发请求到 provider。
    """
    ctx = AiContext(items=[ContextItem(content="hunter2", egress_level=EgressLevel.L5)])
    with pytest.raises(Exception):  # noqa: B017 — 具体异常类型由 Codex impl 决定,契约只规定"必须拒"
        ai_gateway.complete("classify this", ctx, AiOptions(purpose="test"))


@pytest.mark.skip(reason="Codex implements DefaultAiGateway (L4 默认禁,需 opt-in)")
def test_l4_blocked_without_optin(ai_gateway: AiGateway) -> None:
    """L4(样本数据)默认禁;options 显式 opt-in 才允许。"""
    ctx = AiContext(items=[ContextItem(content="row data", egress_level=EgressLevel.L4)])
    with pytest.raises(Exception):  # noqa: B017 — 具体异常类型由 Codex impl 决定,契约只规定"必须拒"
        ai_gateway.complete("infer schema", ctx, AiOptions(purpose="test"))


@pytest.mark.skip(reason="Codex implements DefaultAiGateway (审计)")
def test_each_call_audited(ai_gateway: AiGateway) -> None:
    """★ 契约 §2.7.2:每次调用落 ai_usage(user/purpose/tokens/cost/egress_level)。"""
    pass


@pytest.mark.skip(reason="Codex implements DefaultAiGateway (budget)")
def test_budget_exceeded_rejected(ai_gateway: AiGateway) -> None:
    """budget 超限直接拒,不发请求。"""
    pass


@pytest.mark.skip(reason="Codex implements DefaultAiGateway (streaming)")
def test_stream_complete_yields_chunks(ai_gateway: AiGateway) -> None:
    ctx = AiContext()
    chunks = list(ai_gateway.stream_complete("Hi", ctx, AiOptions(purpose="test")))
    assert len(chunks) > 0
    # 最后一个 chunk finished=True
    assert chunks[-1].finished is True
