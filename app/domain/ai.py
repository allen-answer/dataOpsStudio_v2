from __future__ import annotations

from enum import IntEnum

from pydantic import BaseModel, ConfigDict, Field


class EgressLevel(IntEnum):
    """AI Data Egress 分级(设计稿 §2.7.5)。

    L0 = 产品文档 / 错误码字典(无业务数据)
    L1 = SQL 模板 / 执行计划(不含表注释)
    L2 = schema 结构(表名 / 列名)
    L3 = SQL 原文 / 业务过滤条件
    L4 = 样本数据值                  ★ 默认禁出在线 AI,需 opt-in
    L5 = 密码 / token / 身份证 / 手机号  ★★ 永久禁出,任何配置、任何形态

    Gateway 内置 L5 模式检测(命中即拦截 + 审计),不依赖调用方自觉。
    """

    L0 = 0
    L1 = 1
    L2 = 2
    L3 = 3
    L4 = 4
    L5 = 5


class ContextItem(BaseModel):
    """AI Context 单条(设计稿 §2.7.2)。"""

    model_config = ConfigDict(frozen=True)

    content: str
    egress_level: EgressLevel
    redacted: bool = False


class AiContext(BaseModel):
    items: list[ContextItem] = Field(default_factory=list)


class AiOptions(BaseModel):
    """AI 调用选项(2.0.0 最小集,Codex 实现 provider 时丰满)。"""

    provider: str | None = None
    model: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    purpose: str = "unspecified"  # 落 ai_usage.purpose 审计字段


class AiResponse(BaseModel):
    content: str
    tokens_in: int = 0
    tokens_out: int = 0
    provider: str = "unknown"
    model: str = "unknown"


class AiChunk(BaseModel):
    """Streaming chunk(契约 §3.5 stream_complete 返回值的元素)。"""

    delta: str
    finished: bool = False
