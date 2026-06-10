"""AiGateway Protocol 契约测试(契约 §3.5)。

★ 2.0.0 只做壳:接 1 provider 可调通,但 egress / 脱敏 / 审计 / budget 钩子位
必须留好。本测试用 MockProvider 全覆盖,不发任何真请求。
"""

from __future__ import annotations

import pytest

from app.domain.ai import AiContext, AiOptions, ContextItem, EgressLevel
from app.services.ai import (
    BudgetExceededError,
    BudgetGuard,
    DefaultAiGateway,
    EgressBlockedError,
    LoggingAuditSink,
    MockProvider,
)
from app.services.ai.gateway import AiGateway

pytestmark = pytest.mark.contract


@pytest.fixture
def audit_sink() -> LoggingAuditSink:
    return LoggingAuditSink()


@pytest.fixture
def ai_gateway(audit_sink: LoggingAuditSink) -> AiGateway:
    # L3 max_auto:允许 L0-L3 出站,L4 需 opt-in,L5 永禁——覆盖契约各分支。
    return DefaultAiGateway(
        MockProvider(reply="answer"),
        enabled=True,
        max_auto_egress_level=EgressLevel.L3,
        audit_sink=audit_sink,
    )


def test_complete_returns_response(ai_gateway: AiGateway) -> None:
    ctx = AiContext(items=[ContextItem(content="hello", egress_level=EgressLevel.L0)])
    resp = ai_gateway.complete("Hi", ctx, AiOptions(purpose="test"))
    assert resp.content == "answer"
    assert resp.provider == "mock"


def test_l5_content_blocked(ai_gateway: AiGateway, audit_sink: LoggingAuditSink) -> None:
    """★ 设计稿 §2.7.5:L5(密码/token/PII)在任何形态、任何配置永禁出站。
    Gateway 检测到 L5 命中即拦截 + 审计;不发请求到 provider。
    """
    ctx = AiContext(items=[ContextItem(content="hunter2", egress_level=EgressLevel.L5)])
    with pytest.raises(EgressBlockedError):
        ai_gateway.complete("classify this", ctx, AiOptions(purpose="test"))
    # ★ 拦截也审计了一条 blocked(钩子被调用证据)
    assert audit_sink.records[-1].blocked is True


def test_l4_blocked_without_optin(ai_gateway: AiGateway) -> None:
    """L4(样本数据)默认禁;options 显式 opt-in 才允许。"""
    ctx = AiContext(items=[ContextItem(content="row data", egress_level=EgressLevel.L4)])
    with pytest.raises(EgressBlockedError):
        ai_gateway.complete("infer schema", ctx, AiOptions(purpose="test"))


def test_l4_allowed_with_optin() -> None:
    """l4_requires_optin=False 时 L4 放行(壳级 opt-in 等价)。"""
    gw = DefaultAiGateway(
        MockProvider(reply="ok"),
        max_auto_egress_level=EgressLevel.L3,
        l4_requires_optin=False,
    )
    ctx = AiContext(items=[ContextItem(content="row data", egress_level=EgressLevel.L4)])
    resp = gw.complete("infer", ctx, AiOptions(purpose="test"))
    assert resp.content == "ok"


def test_above_max_auto_level_blocked() -> None:
    """level 超 max_auto_level(且非 L4/L5)被拦截。"""
    gw = DefaultAiGateway(MockProvider(), max_auto_egress_level=EgressLevel.L1)
    ctx = AiContext(items=[ContextItem(content="schema", egress_level=EgressLevel.L2)])
    with pytest.raises(EgressBlockedError):
        gw.complete("q", ctx, AiOptions(purpose="test"))


def test_each_call_audited(ai_gateway: AiGateway, audit_sink: LoggingAuditSink) -> None:
    """★ 契约 §2.7.2:每次调用落审计(user/purpose/tokens/egress_level)。"""
    ctx = AiContext(items=[ContextItem(content="hello", egress_level=EgressLevel.L0)])
    ai_gateway.complete("Hi", ctx, AiOptions(purpose="explain"))
    rec = audit_sink.records[-1]
    assert rec.purpose == "explain"
    assert rec.blocked is False
    assert rec.tokens_out >= 0


def test_audit_never_contains_prompt_text(
    ai_gateway: AiGateway, audit_sink: LoggingAuditSink
) -> None:
    """★ R5:审计只记 hash / 长度,绝不记 prompt 原文。"""
    secret_prompt = "SELECT * FROM payroll WHERE ssn = '12345'"
    ctx = AiContext(items=[ContextItem(content="ctx", egress_level=EgressLevel.L0)])
    ai_gateway.complete(secret_prompt, ctx, AiOptions(purpose="test"))
    rec = audit_sink.records[-1]
    assert secret_prompt not in repr(rec)
    assert rec.prompt_hash.startswith("sha256:")
    assert rec.prompt_len == len(secret_prompt)


def test_budget_exceeded_rejected() -> None:
    """budget 超限直接拒,不发请求(钩子位证明)。"""

    class DenyAllBudget(BudgetGuard):
        def check(self, options: AiOptions) -> None:
            raise BudgetExceededError("over limit")

    audit = LoggingAuditSink()
    provider = MockProvider()
    gw = DefaultAiGateway(provider, budget_guard=DenyAllBudget(), audit_sink=audit)
    ctx = AiContext(items=[ContextItem(content="x", egress_level=EgressLevel.L0)])
    with pytest.raises(BudgetExceededError):
        gw.complete("q", ctx, AiOptions(purpose="test"))
    assert audit.records[-1].block_reason == "budget"


def test_stream_complete_yields_chunks(ai_gateway: AiGateway) -> None:
    ctx = AiContext()
    chunks = list(ai_gateway.stream_complete("Hi", ctx, AiOptions(purpose="test")))
    assert len(chunks) > 0
    assert chunks[-1].finished is True
