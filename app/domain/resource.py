from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ResourceProfile(BaseModel):
    """Per-job resource budget. 见契约 §3.6 + 设计稿 §2.5。

    2.0.0 最小集字段 —— 都 Optional,默认 None 走全局 profile(配置 §4.2)。
    不引入 cpu_weight:2.0.0 无 CPU 调度机制,留死字段会让填了没人读。
    后续版本若加 CPU 调度再扩。
    """

    model_config = ConfigDict(frozen=True)

    memory_limit_mb: int | None = None
    timeout_seconds: int | None = None
