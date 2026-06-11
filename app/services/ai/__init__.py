"""AI Gateway 壳(契约 §3.5、设计稿 §2.7)。

2.0.0 范围:接 1 个 provider 调通 + egress / 脱敏 / 审计三个钩子位留好。
具体 Assist / Copilot 功能 + RedactionPolicy + ai_usage 表是 2.1+。
"""

from __future__ import annotations

from app.services.ai.default_gateway import (
    AI_API_KEY_ENV,
    AI_MODEL_ENV,
    AiGatewayRuntimeConfig,
    BudgetGuard,
    DefaultAiGateway,
    build_gateway,
    build_gateway_from_runtime_config,
)
from app.services.ai.errors import (
    AiDisabledError,
    AiGatewayError,
    BudgetExceededError,
    EgressBlockedError,
    ProviderError,
)
from app.services.ai.gateway import AiGateway
from app.services.ai.hooks import (
    AiUsageRecord,
    AuditSink,
    EgressChecker,
    LevelEgressChecker,
    LoggingAuditSink,
    NoopRedactor,
    Redactor,
)
from app.services.ai.providers import (
    MockProvider,
    OpenAICompatibleProvider,
    Provider,
    UrllibTransport,
)

__all__ = [
    "AI_API_KEY_ENV",
    "AI_MODEL_ENV",
    "AiDisabledError",
    "AiGateway",
    "AiGatewayError",
    "AiGatewayRuntimeConfig",
    "AiUsageRecord",
    "AuditSink",
    "BudgetExceededError",
    "BudgetGuard",
    "DefaultAiGateway",
    "EgressBlockedError",
    "EgressChecker",
    "LevelEgressChecker",
    "LoggingAuditSink",
    "MockProvider",
    "NoopRedactor",
    "OpenAICompatibleProvider",
    "Provider",
    "ProviderError",
    "Redactor",
    "UrllibTransport",
    "build_gateway",
    "build_gateway_from_runtime_config",
]
