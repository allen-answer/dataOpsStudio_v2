"""AiGateway 真 provider ping(契约 §3.5,设计稿 §2.7.2 第 5 条)。

★ 这是手动集成测试:无 env key 自动 skip(契约 §1 pytest markers:integration)。
agent 没有真 key,**不编造调用结果**——CI 不跑,人配齐下列 env 后本地执行验证:

  DATAOPS_AI_API_KEY   provider API key(★ 只从 env 读,不进仓库 / 不建表,R8)
  DATAOPS_AI_ENDPOINT  OpenAI-compatible base URL(如 https://api.openai.com/v1
                       或私有 LLM 网关 base)
  DATAOPS_AI_MODEL     可选,模型名(默认 gpt-4o-mini)

跑法:
  DATAOPS_AI_API_KEY=... DATAOPS_AI_ENDPOINT=... \
    uv run pytest -m integration tests/integration/test_ai_gateway_real_provider.py
"""

from __future__ import annotations

import os

import pytest

from app.config import AiGatewayConfig
from app.domain.ai import AiContext, AiOptions, ContextItem, EgressLevel
from app.services.ai import build_gateway
from app.services.ai.default_gateway import AI_API_KEY_ENV, DefaultAiGateway

pytestmark = pytest.mark.integration

_ENDPOINT_ENV = "DATAOPS_AI_ENDPOINT"


def _gateway_or_skip() -> DefaultAiGateway:
    api_key = os.environ.get(AI_API_KEY_ENV)
    endpoint = os.environ.get(_ENDPOINT_ENV)
    if not api_key or not endpoint:
        pytest.skip(
            f"set {AI_API_KEY_ENV} and {_ENDPOINT_ENV} to run real provider ping "
            "(agent has no key; human runs locally)"
        )
    cfg = AiGatewayConfig(
        enabled=True,
        provider="openai_compatible",
        endpoint=endpoint,
        # L1 足够测一个无业务数据的 ping(产品文档级 prompt)。
        max_auto_egress_level=int(EgressLevel.L1),
    )
    return build_gateway(cfg)


def test_real_provider_ping() -> None:
    gw = _gateway_or_skip()
    ctx = AiContext(
        items=[
            ContextItem(
                content="You are a connectivity probe. Reply with the single word OK.",
                egress_level=EgressLevel.L0,
            )
        ]
    )
    resp = gw.complete("ping", ctx, AiOptions(purpose="connectivity_probe", max_tokens=8))
    assert isinstance(resp.content, str)
    assert resp.content != ""
