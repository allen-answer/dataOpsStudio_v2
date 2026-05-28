from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PlanNode(BaseModel):
    """EXPLAIN 计划树节点。

    2.0.0 占位类型 —— 实际字段由 adapter 实现决定(Oracle/MySQL/DM/DB2
    EXPLAIN 输出差异巨大,设计稿/契约 未规定细节)。Codex 实现 adapter.explain
    时按各方言丰满。Step 1 只占位以便 DatabaseAdapter.explain 签名能 import。

    见契约 §3.2、设计稿 §2.9。
    """

    operation: str
    children: list[PlanNode] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
