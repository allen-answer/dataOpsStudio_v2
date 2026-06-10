"""AiGateway 三个钩子位:egress 检查 / 脱敏 / 审计(契约 §3.5、设计稿 §2.7.2)。

★ 2.0.0 只留钩子位 + no-op / 最小默认实现,**不实现具体策略**——具体的
RedactionPolicy / per-feature budget / ai_usage 表落库是 2.1+ 的工作。
2.0.0 的目标:DefaultAiGateway 每次调用都**确实调到这三个钩子**(单测证明),
这样 2.1+ 接真实现时调用点已经在,不用回头改 Gateway 主流程。

钩子分工:
- EgressChecker:按 EgressLevel 决定 context item 能否出站(L5 永禁,L4 需 opt-in)。
- Redactor:对允许出站的内容做脱敏(SQL / 表名 / 列名 / 样本)。2.0.0 default no-op。
- AuditSink:每次调用记一条审计(user / purpose / tokens / egress_level)。
  ★ R5:审计**不记 prompt/响应原文**,只记 hash / 长度 / token 计数。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Protocol

import structlog

from app.domain.ai import AiContext, ContextItem, EgressLevel
from app.services.ai.errors import EgressBlockedError

_log = structlog.get_logger(__name__)


# ── 1. Egress 检查钩子 ────────────────────────────────────────────────────────


class EgressChecker(Protocol):
    """决定单条 context item 能否出站(设计稿 §2.7.5)。"""

    def check(self, item: ContextItem, *, l4_optin: bool) -> None:
        """命中禁止规则时 raise EgressBlockedError;通过则返回 None。"""
        ...


@dataclass(frozen=True)
class LevelEgressChecker:
    """2.0.0 默认 egress 检查:按 max_auto_egress_level 阈值放行。

    规则(设计稿 §2.7.5):
    - L5 永久禁止,任何配置都拦截。
    - L4 默认禁,仅当 options 显式 opt-in 时放行。
    - 其余:level <= max_auto_level 放行,否则拦截。

    ★ 这是壳级阈值策略;按表名/正则等内容级 L5 探测(密码字段名 / 身份证 /
    手机号)是 2.1+ 的具体策略,此处只做等级闸门 + 留扩展点。
    """

    max_auto_level: EgressLevel = EgressLevel.L0

    def check(self, item: ContextItem, *, l4_optin: bool) -> None:
        level = item.egress_level
        if level == EgressLevel.L5:
            raise EgressBlockedError(level, self.max_auto_level)
        if level == EgressLevel.L4:
            if not l4_optin:
                raise EgressBlockedError(level, self.max_auto_level)
            return
        if level > self.max_auto_level:
            raise EgressBlockedError(level, self.max_auto_level)


# ── 2. 脱敏钩子 ───────────────────────────────────────────────────────────────


class Redactor(Protocol):
    """对允许出站的内容做脱敏(设计稿 §2.7.2 第 2 条)。"""

    def redact(self, context: AiContext) -> AiContext:
        """返回脱敏后的 context(可能逐 item 标记 redacted=True)。"""
        ...


@dataclass(frozen=True)
class NoopRedactor:
    """2.0.0 默认脱敏:no-op(原样返回)。

    具体 RedactionPolicy(SQL 字面量打码 / 表名映射 / 样本截断)是 2.1+ 工作。
    钩子位在此,Gateway 主流程已调用它,接真实现时无需改主流程。
    """

    def redact(self, context: AiContext) -> AiContext:
        return context


# ── 3. 审计钩子 ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AiUsageRecord:
    """一次 AI 调用的审计记录(设计稿 §2.7.2 第 3 条:落 ai_usage 表)。

    ★ R5:**不含 prompt / 响应原文**——只记 hash / 长度 / token 计数 / 等级。
    2.0.0 无 ai_usage 表(契约 §5 范围),用 dataclass 承载,LoggingAuditSink
    打进脱敏日志;2.1+ 接真实表时新增 DbAuditSink 实现同一 Protocol。
    """

    purpose: str
    provider: str
    model: str
    user_id: str | None
    project_id: str | None
    prompt_hash: str
    prompt_len: int
    max_egress_level: EgressLevel
    tokens_in: int = 0
    tokens_out: int = 0
    blocked: bool = False
    block_reason: str | None = None


class AuditSink(Protocol):
    """承接每次 AI 调用的审计记录(设计稿 §2.7.2 第 3 条)。"""

    def record(self, usage: AiUsageRecord) -> None: ...


@dataclass
class LoggingAuditSink:
    """2.0.0 默认审计:打进 structlog(脱敏 processor 兜底,R5)。

    记录留在 records 列表供单测断言"每次调用都审计"。生产形态由 structlog
    JSON 输出到 stdout / journald;2.1+ 引入 ai_usage 表后换 DbAuditSink。
    """

    records: list[AiUsageRecord] = field(default_factory=list)

    def record(self, usage: AiUsageRecord) -> None:
        self.records.append(usage)
        # ★ R5:只记 hash / 长度 / token / 等级,绝不记 prompt / 响应原文。
        _log.info(
            "ai_usage",
            purpose=usage.purpose,
            provider=usage.provider,
            model=usage.model,
            user_id=usage.user_id,
            project_id=usage.project_id,
            prompt_hash=usage.prompt_hash,
            prompt_len=usage.prompt_len,
            max_egress_level=int(usage.max_egress_level),
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
            blocked=usage.blocked,
            block_reason=usage.block_reason,
        )


def hash_prompt(prompt: str) -> str:
    """prompt 内容指纹(R5:审计只记指纹,不记原文)。"""
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"
