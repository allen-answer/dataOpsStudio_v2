"""AI Gateway 壳 单元测试(契约 §3.5、设计稿 §2.7)。

覆盖:
- OpenAICompatibleProvider 用 mock transport 解析响应(不发真请求)。
- 脱敏钩子确实被 Gateway 调用(NoopRedactor + 自定义 Redactor 计数)。
- build_gateway 从 config 装配(默认关 / openai_compatible + env key)。
- provider 错误降级为 ProviderError(AiGatewayError 子类)。

★ 真 provider ping 见 test_ai_gateway_integration.py(@pytest.mark.integration,
无 env key 自动 skip)。本文件 0 网络。
"""

from __future__ import annotations

import pytest

from app.config import AiGatewayConfig
from app.domain.ai import AiContext, AiOptions, ContextItem, EgressLevel
from app.services.ai import (
    AiDisabledError,
    AiGatewayRuntimeConfig,
    DefaultAiGateway,
    MockProvider,
    OpenAICompatibleProvider,
    ProviderError,
    build_gateway,
    build_gateway_from_runtime_config,
)
from app.services.ai.default_gateway import AI_API_KEY_ENV, AI_MODEL_ENV
from app.services.ai.hooks import LevelEgressChecker, NoopRedactor


class _FakeTransport:
    """记录请求并返回固定 OpenAI-compatible 响应,不发网络。"""

    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.last_url: str = ""
        self.last_headers: dict[str, str] = {}
        self.last_body: dict[str, object] = {}

    def post_json(
        self, url: str, headers: dict[str, str], body: dict[str, object]
    ) -> dict[str, object]:
        self.last_url = url
        self.last_headers = headers
        self.last_body = body
        return self.response


_OPENAI_RESPONSE: dict[str, object] = {
    "model": "test-model",
    "choices": [{"message": {"role": "assistant", "content": "hello back"}}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 3},
}


def test_openai_compatible_parses_response() -> None:
    transport = _FakeTransport(_OPENAI_RESPONSE)
    provider = OpenAICompatibleProvider(
        api_key="sk-test-not-real",
        endpoint="https://llm.example.invalid/v1",
        model="default-model",
        transport=transport,
    )
    ctx = AiContext(items=[ContextItem(content="system ctx", egress_level=EgressLevel.L0)])
    resp = provider.complete("hi", ctx, AiOptions(purpose="test"))

    assert resp.content == "hello back"
    assert resp.tokens_in == 12
    assert resp.tokens_out == 3
    assert resp.provider == "openai_compatible"
    # URL 拼接:base /v1 → /v1/chat/completions
    assert transport.last_url.endswith("/chat/completions")
    # context item 进 system message,prompt 进 user message
    msgs = transport.last_body["messages"]
    assert isinstance(msgs, list)
    assert msgs[0] == {"role": "system", "content": "system ctx"}
    assert msgs[-1] == {"role": "user", "content": "hi"}
    # bearer 头带上(但绝不在日志/响应里回显)
    assert transport.last_headers["authorization"].startswith("Bearer ")


def test_openai_compatible_endpoint_already_full_path() -> None:
    transport = _FakeTransport(_OPENAI_RESPONSE)
    provider = OpenAICompatibleProvider(
        api_key="k",
        endpoint="https://llm.example.invalid/v1/chat/completions",
        model="m",
        transport=transport,
    )
    provider.complete("hi", AiContext(), AiOptions())
    assert transport.last_url == "https://llm.example.invalid/v1/chat/completions"


def test_openai_compatible_bad_response_raises_provider_error() -> None:
    transport = _FakeTransport({"unexpected": "shape"})
    provider = OpenAICompatibleProvider(
        api_key="k", endpoint="https://x.invalid", model="m", transport=transport
    )
    with pytest.raises(ProviderError):
        provider.complete("hi", AiContext(), AiOptions())


def test_openai_compatible_empty_content_raises_provider_error() -> None:
    response: dict[str, object] = {
        "model": "test-model",
        "choices": [{"message": {"role": "assistant", "content": "   "}}],
    }
    transport = _FakeTransport(response)
    provider = OpenAICompatibleProvider(
        api_key="k", endpoint="https://x.invalid", model="m", transport=transport
    )

    with pytest.raises(ProviderError, match="empty"):
        provider.complete("hi", AiContext(), AiOptions())


def test_redactor_hook_is_called() -> None:
    """★ 脱敏钩子位:Gateway 主流程对每次放行调用确实调到 Redactor。"""

    class CountingRedactor(NoopRedactor):
        def __init__(self) -> None:
            self.calls = 0

        def redact(self, context: AiContext) -> AiContext:
            self.calls += 1
            return context

    redactor = CountingRedactor()
    gw = DefaultAiGateway(MockProvider(), redactor=redactor, max_auto_egress_level=EgressLevel.L1)
    ctx = AiContext(items=[ContextItem(content="x", egress_level=EgressLevel.L0)])
    gw.complete("q", ctx, AiOptions(purpose="test"))
    assert redactor.calls == 1


def test_disabled_gateway_raises() -> None:
    gw = DefaultAiGateway(MockProvider(), enabled=False)
    with pytest.raises(AiDisabledError):
        gw.complete("q", AiContext(), AiOptions())


def test_level_egress_checker_l5_always_blocked() -> None:
    """★ L5 即便 max_auto 拉到 L5 也禁(永久禁止)。"""
    checker = LevelEgressChecker(max_auto_level=EgressLevel.L5)
    item = ContextItem(content="pw", egress_level=EgressLevel.L5)
    with pytest.raises(Exception):  # noqa: B017
        checker.check(item, l4_optin=True)


def test_build_gateway_disabled_by_default() -> None:
    """默认 config(enabled=False)→ 可构造,但 complete 抛 AiDisabledError。"""
    gw = build_gateway(AiGatewayConfig())
    with pytest.raises(AiDisabledError):
        gw.complete("q", AiContext(), AiOptions())


def test_build_gateway_falls_back_to_mock_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings: list[dict[str, object]] = []

    class _Log:
        def warning(self, event: str, **kwargs: object) -> None:
            warnings.append({"event": event, **kwargs})

    monkeypatch.delenv(AI_API_KEY_ENV, raising=False)
    monkeypatch.setattr("app.services.ai.default_gateway._log", _Log())
    cfg = AiGatewayConfig(enabled=True, provider="openai_compatible", endpoint="https://x.invalid")
    gw = build_gateway(cfg)
    # 无 key → 回落 mock,complete 可跑通(不报缺 key)
    resp = gw.complete("hi", AiContext(), AiOptions(purpose="test"))
    assert resp.provider == "mock"
    assert warnings == [
        {
            "event": "ai gateway falling back to mock provider",
            "provider": "openai_compatible",
            "has_endpoint": True,
            "has_api_key": False,
        }
    ]


def test_build_gateway_uses_openai_with_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """配齐 provider/endpoint + env key → 装配真 provider(★ key 只从 env 读)。"""
    monkeypatch.setenv(AI_API_KEY_ENV, "sk-test-not-real")
    monkeypatch.setenv(AI_MODEL_ENV, "my-model")
    cfg = AiGatewayConfig(
        enabled=True, provider="openai_compatible", endpoint="https://llm.example.invalid/v1"
    )
    gw = build_gateway(cfg)
    # 透过私有属性确认装配的是 OpenAICompatibleProvider(不发请求)
    provider = gw._provider  # 白盒断言装配结果
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.model == "my-model"


def test_build_gateway_uses_runtime_config_key_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(AI_API_KEY_ENV, raising=False)
    cfg = AiGatewayRuntimeConfig(
        enabled=True,
        provider="openai_compatible",
        endpoint="https://llm.example.invalid/v1",
        model="db-model",
        api_key="not-a-real-test-key",
        max_auto_egress_level=1,
    )

    gw = build_gateway_from_runtime_config(cfg)

    provider = gw._provider
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.model == "db-model"
