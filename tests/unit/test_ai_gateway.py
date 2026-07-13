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

import urllib.error
import urllib.request

import pytest

from app.config import AiGatewayConfig
from app.domain.ai import AiContext, AiOptions, ContextItem, EgressLevel, ReasoningMode
from app.services.ai import (
    AiDisabledError,
    AiGatewayRuntimeConfig,
    DefaultAiGateway,
    MockProvider,
    OpenAICompatibleProvider,
    ProviderError,
    UrllibTransport,
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


def test_openai_compatible_empty_content_is_preserved_for_caller_diagnostics() -> None:
    response: dict[str, object] = {
        "model": "test-model",
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "   "}}],
    }
    provider = OpenAICompatibleProvider(
        api_key="not-a-real-key",
        endpoint="https://example.invalid",
        model="test-model",
        transport=_FakeTransport(response),
    )

    result = provider.complete("hi", AiContext(), AiOptions())

    assert result.content == ""
    assert result.reasoning_chars == 0


def test_openai_compatible_normalizes_reasoning_finish_and_usage() -> None:
    transport = _FakeTransport(
        {
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {
                        "role": "assistant",
                        "reasoning_content": "private chain that must not escape",
                        "content": "",
                    },
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 90, "total_tokens": 100},
        }
    )
    provider = OpenAICompatibleProvider(
        api_key="not-a-real-key",
        endpoint="https://example.invalid/v1",
        model="deepseek-v4-pro",
        transport=transport,
    )

    response = provider.complete("query", AiContext(), AiOptions(max_tokens=100))

    assert response.content == ""
    assert response.finish_reason == "length"
    assert response.reasoning_chars == len("private chain that must not escape")
    assert response.tokens_total == 100
    assert "private chain" not in response.model_dump_json()


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (ReasoningMode.ENABLED, {"type": "enabled"}),
        (ReasoningMode.DISABLED, {"type": "disabled"}),
    ],
)
def test_deepseek_model_maps_reasoning_mode_to_thinking(
    mode: ReasoningMode, expected: dict[str, str]
) -> None:
    transport = _FakeTransport(_OPENAI_RESPONSE)
    provider = OpenAICompatibleProvider(
        api_key="not-a-real-key",
        endpoint="https://example.invalid/v1",
        model="deepseek-v4-pro",
        transport=transport,
    )

    provider.complete("query", AiContext(), AiOptions(reasoning_mode=mode))

    assert transport.last_body["thinking"] == expected


def test_generic_model_does_not_receive_vendor_thinking_field() -> None:
    transport = _FakeTransport(_OPENAI_RESPONSE)
    provider = OpenAICompatibleProvider(
        api_key="not-a-real-key",
        endpoint="https://example.invalid/v1",
        model="generic-model",
        transport=transport,
    )

    provider.complete("query", AiContext(), AiOptions(reasoning_mode=ReasoningMode.DISABLED))

    assert "thinking" not in transport.last_body


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "provider_auth_failed"), (403, "provider_auth_failed"), (429, "provider_rate_limited")],
)
def test_urllib_transport_classifies_http_without_exposing_body(
    monkeypatch: pytest.MonkeyPatch, status: int, code: str
) -> None:
    error = urllib.error.HTTPError(
        "https://example.invalid", status, "provider secret body", {}, None
    )
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(ProviderError) as caught:
        UrllibTransport().post_json("https://example.invalid", {}, {})

    assert caught.value.diagnostic_code == code
    assert caught.value.status_code == status
    assert "provider secret body" not in str(caught.value)


@pytest.mark.parametrize(
    ("reason", "code"),
    [(TimeoutError(), "provider_timeout"), (OSError("offline"), "provider_unreachable")],
)
def test_urllib_transport_classifies_network_failure(
    monkeypatch: pytest.MonkeyPatch, reason: Exception, code: str
) -> None:
    error = urllib.error.URLError(reason)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(ProviderError) as caught:
        UrllibTransport().post_json("https://example.invalid", {}, {})

    assert caught.value.diagnostic_code == code


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
