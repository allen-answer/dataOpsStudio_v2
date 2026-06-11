"""AI provider 抽象 + 2.0.0 两个实现(契约 §3.5、设计稿 §2.7.2 第 5 条)。

2.0.0 范围:能接 **1 个 provider 调通** 即可。选 OpenAI-compatible chat
completions——它是最薄的真协议(单个 POST /chat/completions,JSON 进 JSON 出),
且覆盖最多 on-prem 私有 LLM(vLLM / Ollama / LM Studio / 文心兼容层等都讲它),
契合"on-prem 可配私有 LLM"(设计稿 §2.7.2 第 5 条)。

★ 不新增依赖:HTTP 用 stdlib urllib(Karpathy #2 简单优先)。Transport 可注入,
单测用 mock transport 全覆盖,**绝不发真请求**。

★ R8 / 契约 §5:API key 由 Gateway 工厂注入,来源只能是 SecretStore
或环境变量 fallback;provider 不负责读取配置,也不记录 key。

★ R5:provider 不打 prompt / 响应原文进日志。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from app.domain.ai import AiContext, AiOptions, AiResponse
from app.services.ai.errors import ProviderError


class Provider(Protocol):
    """provider 调用抽象。Gateway 负责 egress / 脱敏 / 审计,provider 只管发请求。"""

    name: str

    def complete(self, prompt: str, context: AiContext, options: AiOptions) -> AiResponse: ...


# ── Mock provider(测试 + 默认关时占位)────────────────────────────────────────


@dataclass
class MockProvider:
    """合成响应 provider,供单测 / dev 使用,不发任何网络请求。

    回声式:把 prompt 末尾截断回显,token 计数用粗略词数。让 Gateway 全链路
    (egress → 脱敏 → provider → 审计)可在无真 key、无网络下端到端测通。
    """

    name: str = "mock"
    model: str = "mock-model"
    reply: str = "ok"

    def complete(self, prompt: str, context: AiContext, options: AiOptions) -> AiResponse:
        model = options.model or self.model
        tokens_in = len(prompt.split()) + sum(len(i.content.split()) for i in context.items)
        return AiResponse(
            content=self.reply,
            tokens_in=tokens_in,
            tokens_out=len(self.reply.split()),
            provider=self.name,
            model=model,
        )


# ── OpenAI-compatible provider(真 provider,stdlib urllib)──────────────────────


class HttpTransport(Protocol):
    """可注入的 HTTP transport——单测替换为 mock,绝不发真请求。"""

    def post_json(
        self, url: str, headers: dict[str, str], body: dict[str, object]
    ) -> dict[str, object]: ...


class UrllibTransport:
    """stdlib urllib 实现(默认)。不引第三方 HTTP 库。"""

    def __init__(self, timeout_seconds: float = 30.0) -> None:
        self._timeout = timeout_seconds

    def post_json(
        self, url: str, headers: dict[str, str], body: dict[str, object]
    ) -> dict[str, object]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        for key, value in headers.items():
            req.add_header(key, value)
        req.add_header("content-type", "application/json")
        try:
            # url 来自受信配置(endpoint),非用户输入。
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:  # pragma: no cover - 真网络分支
            # ★ R5:不回显响应体(可能含 provider 侧 echo);只记状态码。
            raise ProviderError(f"provider HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:  # pragma: no cover - 真网络分支
            raise ProviderError("provider unreachable") from exc
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:  # pragma: no cover - 真网络分支
            raise ProviderError("provider returned non-JSON body") from exc
        if not isinstance(parsed, dict):
            raise ProviderError("provider returned non-object JSON")
        return parsed


@dataclass
class OpenAICompatibleProvider:
    """OpenAI-compatible /chat/completions provider(设计稿 §2.7.2 第 5 条)。

    endpoint 形如 https://api.openai.com/v1 或私有 LLM 网关 base URL。
    api_key 由 Gateway 工厂注入(R8:不进配置文件 / 不进明文字段)。
    """

    api_key: str
    endpoint: str
    model: str
    name: str = "openai_compatible"
    transport: HttpTransport | None = None

    def __post_init__(self) -> None:
        if self.transport is None:
            self.transport = UrllibTransport()

    def _url(self) -> str:
        base = self.endpoint.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def complete(self, prompt: str, context: AiContext, options: AiOptions) -> AiResponse:
        model = options.model or self.model
        messages: list[dict[str, str]] = []
        for item in context.items:
            messages.append({"role": "system", "content": item.content})
        messages.append({"role": "user", "content": prompt})

        body: dict[str, object] = {"model": model, "messages": messages}
        if options.max_tokens is not None:
            body["max_tokens"] = options.max_tokens
        if options.temperature is not None:
            body["temperature"] = options.temperature

        headers = {"authorization": f"Bearer {self.api_key}"}
        assert self.transport is not None  # __post_init__ 保证
        payload = self.transport.post_json(self._url(), headers, body)
        return _parse_chat_completion(payload, fallback_provider=self.name, fallback_model=model)


def _parse_chat_completion(
    payload: dict[str, object], *, fallback_provider: str, fallback_model: str
) -> AiResponse:
    """解析 OpenAI-compatible /chat/completions 响应。

    容错:缺字段不抛(壳级取最小可用),只在 choices 结构完全不对时报错。
    """
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderError("provider response missing choices")
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise ProviderError("provider response content not a string")

    usage_raw = payload.get("usage")
    usage = usage_raw if isinstance(usage_raw, dict) else {}
    tokens_in = _as_int(usage.get("prompt_tokens"))
    tokens_out = _as_int(usage.get("completion_tokens"))
    model_raw = payload.get("model")
    model = model_raw if isinstance(model_raw, str) else fallback_model
    return AiResponse(
        content=content,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        provider=fallback_provider,
        model=model,
    )


def _as_int(value: object) -> int:
    """token 计数容错转 int(缺失 / 非数字 → 0)。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0
