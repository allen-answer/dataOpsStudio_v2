"""DefaultAiGateway —— 2.0.0 壳级 AiGateway 实现(契约 §3.5、设计稿 §2.7.2)。

壳的目标:
1. 接 1 个 provider 调通(OpenAICompatibleProvider,或 MockProvider 测试)。
2. ★ egress 检查 / 脱敏 / 审计三个钩子位**每次调用都确实走到**(单测证明)。
   具体策略(RedactionPolicy / ai_usage 表 / per-feature budget)是 2.1+。

调用主流程(complete):
    1. enabled 检查 → 关则 AiDisabledError(portable / on-prem 默认关)
    2. budget 钩子(2.0.0 默认无上限,留位)
    3. egress 检查:逐 item 过 EgressChecker(L5 永禁 / L4 需 opt-in / 阈值)
       —— 任一拦截:审计一条 blocked + raise EgressBlockedError(不发 provider)
    4. 脱敏:Redactor.redact(2.0.0 no-op)
    5. provider.complete(发请求)
    6. 审计:AuditSink.record(★ R5 只记 hash / 长度 / token / 等级)

stream_complete:2.0.0 壳级把 complete 结果包成单个 finished chunk。真流式
(SSE 透传 provider delta)留 2.1+ AI Assist。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass

import structlog

from app.config import AiGatewayConfig
from app.domain.ai import AiChunk, AiContext, AiOptions, AiResponse, EgressLevel
from app.services.ai.errors import AiDisabledError, BudgetExceededError
from app.services.ai.hooks import (
    AiUsageRecord,
    AuditSink,
    EgressChecker,
    LevelEgressChecker,
    LoggingAuditSink,
    NoopRedactor,
    Redactor,
    hash_prompt,
)
from app.services.ai.providers import (
    MockProvider,
    OpenAICompatibleProvider,
    Provider,
)

# ★ R8 / 契约 §5:环境变量仅作为 fallback;DB 配置中的 API key 只经 SecretStore 进入。
AI_API_KEY_ENV = "DATAOPS_AI_API_KEY"
AI_MODEL_ENV = "DATAOPS_AI_MODEL"
DEFAULT_MODEL = "gpt-4o-mini"

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class AiGatewayRuntimeConfig:
    enabled: bool = False
    provider: str | None = None
    endpoint: str | None = None
    model: str | None = None
    api_key: str | None = None
    max_auto_egress_level: int = 0
    l4_requires_optin: bool = True


class BudgetGuard:
    """budget 钩子位(设计稿 §2.7.2 第 4 条)。

    2.0.0 默认无上限(留位)。2.1+ 接 per-user / per-project / per-tenant 上限。
    """

    def check(self, options: AiOptions) -> None:
        """超限时 raise BudgetExceededError;2.0.0 默认不拦。"""
        return None


class DefaultAiGateway:
    """壳级 AiGateway 实现(契约 §3.5 AiGateway Protocol)。

    钩子位均可注入(便于 2.1+ 替真实现 + 单测断言被调用);默认给最小实现。
    """

    def __init__(
        self,
        provider: Provider,
        *,
        enabled: bool = True,
        max_auto_egress_level: EgressLevel = EgressLevel.L0,
        l4_requires_optin: bool = True,
        egress_checker: EgressChecker | None = None,
        redactor: Redactor | None = None,
        audit_sink: AuditSink | None = None,
        budget_guard: BudgetGuard | None = None,
    ) -> None:
        self._provider = provider
        self._enabled = enabled
        self._l4_requires_optin = l4_requires_optin
        self._egress = egress_checker or LevelEgressChecker(max_auto_level=max_auto_egress_level)
        self._redactor = redactor or NoopRedactor()
        self._audit = audit_sink or LoggingAuditSink()
        self._budget = budget_guard or BudgetGuard()

    def complete(self, prompt: str, context: AiContext, options: AiOptions) -> AiResponse:
        if not self._enabled:
            raise AiDisabledError("AI gateway is disabled")

        max_level = _max_level(context)

        # budget 钩子(留位)
        try:
            self._budget.check(options)
        except BudgetExceededError:
            self._audit.record(
                self._usage(options, prompt, max_level, blocked=True, block_reason="budget")
            )
            raise

        # egress 检查钩子——任一 item 被拦截即审计 blocked + 抛出,不发 provider
        l4_optin = not self._l4_requires_optin
        try:
            for item in context.items:
                self._egress.check(item, l4_optin=l4_optin)
        except Exception as exc:
            self._audit.record(
                self._usage(
                    options, prompt, max_level, blocked=True, block_reason=type(exc).__name__
                )
            )
            raise

        # 脱敏钩子
        clean_context = self._redactor.redact(context)

        # provider 调用
        response = self._provider.complete(prompt, clean_context, options)

        # 审计钩子(★ R5:只记 hash / 长度 / token / 等级)
        self._audit.record(
            self._usage(
                options,
                prompt,
                max_level,
                tokens_in=response.tokens_in,
                tokens_out=response.tokens_out,
                provider=response.provider,
                model=response.model,
            )
        )
        return response

    def stream_complete(
        self, prompt: str, context: AiContext, options: AiOptions
    ) -> Iterator[AiChunk]:
        """2.0.0 壳级:复用 complete,把结果包成单个 finished chunk。

        egress / 脱敏 / 审计在 complete 内已走过。真流式 SSE 透传留 2.1+。
        """
        response = self.complete(prompt, context, options)
        yield AiChunk(delta=response.content, finished=True)

    def _usage(
        self,
        options: AiOptions,
        prompt: str,
        max_level: EgressLevel,
        *,
        tokens_in: int = 0,
        tokens_out: int = 0,
        provider: str | None = None,
        model: str | None = None,
        blocked: bool = False,
        block_reason: str | None = None,
    ) -> AiUsageRecord:
        provider_name = provider or getattr(self._provider, "name", "unknown")
        return AiUsageRecord(
            purpose=options.purpose,
            provider=str(provider_name),
            model=model or options.model or "unknown",
            user_id=None,  # 2.0.0 壳无调用方身份透传;2.1+ AiOptions / context 带 user/project
            project_id=None,
            prompt_hash=hash_prompt(prompt),
            prompt_len=len(prompt),
            max_egress_level=max_level,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            blocked=blocked,
            block_reason=block_reason,
        )


def _max_level(context: AiContext) -> EgressLevel:
    """context 中最高 egress 等级(空 context 视为 L0)。"""
    if not context.items:
        return EgressLevel.L0
    return max((item.egress_level for item in context.items), default=EgressLevel.L0)


def build_gateway(
    config: AiGatewayConfig,
    *,
    provider: Provider | None = None,
    audit_sink: AuditSink | None = None,
) -> DefaultAiGateway:
    """从 AiGatewayConfig 装配 DefaultAiGateway(进程启动时调一次)。

    provider 选择:
    - config.provider == "openai_compatible" 且配了 endpoint:用真 provider,
      API key 从 DATAOPS_AI_API_KEY 环境变量读(兼容旧部署)。
    - 其它(含未配 / mock / 缺 key):回落 MockProvider,保证默认关 / 无 key 时
      Gateway 仍可构造(complete 会因 enabled=False 抛 AiDisabledError)。

    显式传入 provider 时优先用传入的(便于测试 / 私有实现)。
    """
    if provider is None:
        provider = _provider_from_config(config)
    return DefaultAiGateway(
        provider,
        enabled=config.enabled,
        max_auto_egress_level=EgressLevel(config.max_auto_egress_level),
        l4_requires_optin=config.l4_requires_optin,
        audit_sink=audit_sink,
    )


def build_gateway_from_runtime_config(
    config: AiGatewayRuntimeConfig,
    *,
    provider: Provider | None = None,
    audit_sink: AuditSink | None = None,
) -> DefaultAiGateway:
    if provider is None:
        provider = _provider_from_values(
            provider_name=config.provider,
            endpoint=config.endpoint,
            # ast-grep-ignore: r2-no-plaintext-password-access
            api_key=config.api_key,
            model=config.model,
        )
    return DefaultAiGateway(
        provider,
        enabled=config.enabled,
        max_auto_egress_level=EgressLevel(config.max_auto_egress_level),
        l4_requires_optin=config.l4_requires_optin,
        audit_sink=audit_sink,
    )


def _provider_from_config(config: AiGatewayConfig) -> Provider:
    return _provider_from_values(
        provider_name=config.provider,
        endpoint=config.endpoint,
        api_key=os.environ.get(AI_API_KEY_ENV),
        model=os.environ.get(AI_MODEL_ENV),
    )


def _provider_from_values(
    *,
    provider_name: str | None,
    endpoint: str | None,
    api_key: str | None,
    model: str | None,
) -> Provider:
    if provider_name == "openai_compatible" and endpoint and api_key:
        return OpenAICompatibleProvider(
            api_key=api_key,
            endpoint=endpoint,
            model=model or DEFAULT_MODEL,
        )
    if provider_name == "openai_compatible":
        _log.warning(
            "ai gateway falling back to mock provider",
            provider=provider_name,
            has_endpoint=bool(endpoint),
            has_api_key=bool(api_key),
        )
    return MockProvider()
