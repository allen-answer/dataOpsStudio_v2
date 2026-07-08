"""L-1:血缘方言自动识别(纯启发式 + sqlglot 试解析兜底)。

设计边界:
- **用户显式指定的 dialect 永远优先**——本模块只在请求方言为 ``"auto"`` 哨兵时介入,
  从不覆盖显式方言(见 :func:`resolve_dialect`)。
- 纯静态、无 DB、可单测:特征 token 打分(唯一胜出即采纳)+ 试解析兜底(平票时
  按候选顺序试,首个无解析错误的胜出);无任何特征则回落到 ``default``(现行为)。
- db2 **不加入候选**(GA 未 Certified,决策见差距矩阵 L-1);dm 与 oracle 语法同源,
  同样不进自动候选(默认可显式指定或作为 tie-break default 传入)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot

from app.domain.lineage.dialects import register_lineage_dialects

# 自动识别哨兵:请求方言等于该值(大小写不敏感)才触发识别。
AUTO_DIALECT = "auto"

# 自动识别候选(打分 + 试解析)。顺序即平票 tie-break 优先级。
# dm/db2 不进候选(见模块 docstring)。
DETECT_CANDIDATES: tuple[str, ...] = ("oracle", "tsql", "postgres", "mysql")

# 每方言的特征正则(命中一条计 1 分)。刻意选各方言独有 / 强指示的 token,
# 避免跨方言通用语法造成噪声。大小写不敏感。
_FEATURE_PATTERNS: dict[str, tuple[str, ...]] = {
    "oracle": (
        r"\bNVL\s*\(",
        r"\bROWNUM\b",
        r"\bSYSDATE\b",
        r"\bFROM\s+DUAL\b",
        r"\bCONNECT\s+BY\b",
        r"\bLISTAGG\s*\(",
        r"\bDECODE\s*\(",
        r"\bVARCHAR2\b",
        r"\(\s*\+\s*\)",  # (+) 外连接
    ),
    "tsql": (
        r"\bSELECT\s+TOP\s+\(?\s*\d",
        r"\bISNULL\s*\(",
        r"\bNVARCHAR\b",
        r"\bGETDATE\s*\(\s*\)",
        r"\bDATEADD\s*\(",
        r"\bDATEDIFF\s*\(",
        r"@\w+",  # 变量 / 参数
        r"\[[^\]]+\]",  # 方括号标识符
        r"\bLEN\s*\(",
    ),
    "postgres": (
        r"::\s*\w+",  # :: 类型转换
        r"\bRETURNING\b",
        r"\bILIKE\b",
        r"\bSERIAL\b",
        r"\bJSONB\b",
        r"\bNOW\s*\(\s*\)",
        r"\bDISTINCT\s+ON\b",
        r"\$\$",  # dollar-quoting
        r"\bGENERATE_SERIES\s*\(",
    ),
    "mysql": (
        r"`[^`]+`",  # 反引号标识符
        r"\bAUTO_INCREMENT\b",
        r"\bIFNULL\s*\(",
        r"\bUNSIGNED\b",
        r"\bENGINE\s*=",
        r"\bLIMIT\s+\d+\s*,\s*\d+",  # LIMIT offset,count
        r"\bGROUP_CONCAT\s*\(",
        r"\bSTRAIGHT_JOIN\b",
    ),
}

_COMPILED_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    dialect: tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)
    for dialect, patterns in _FEATURE_PATTERNS.items()
}


@dataclass(frozen=True)
class DialectDetection:
    """方言解析结果。``auto_detected`` 为真表示经识别得出(非显式指定)。"""

    dialect: str
    auto_detected: bool
    method: str  # "explicit" | "heuristic" | "trial_parse" | "default"
    default: str
    scores: dict[str, int] = field(default_factory=dict)

    def as_summary(self) -> dict[str, object]:
        """进 parse_summary / files_report 的可序列化摘要。"""
        return {
            "dialect": self.dialect,
            "auto_detected": self.auto_detected,
            "method": self.method,
            "default": self.default,
            "scores": dict(self.scores),
        }


def _heuristic_scores(sql_text: str) -> dict[str, int]:
    return {
        dialect: sum(1 for pattern in patterns if pattern.search(sql_text))
        for dialect, patterns in _COMPILED_PATTERNS.items()
    }


def _parses_cleanly(sql_text: str, dialect: str) -> bool:
    try:
        sqlglot.parse(sql_text, read=dialect)
    except Exception:
        return False
    return True


def detect_dialect(sql_text: str, *, default: str = "oracle") -> DialectDetection:
    """从 SQL 文本识别方言。

    策略:
    1. 特征打分,**唯一**最高分(>0)直接胜出(method=heuristic)。
    2. 平票(多个并列最高,>0):按 :data:`DETECT_CANDIDATES` 顺序试解析,
       首个无解析错误的胜出(method=trial_parse);全部报错则回落 default。
    3. 无任何特征(全 0):回落 default(method=default,即现行为)。
    """
    register_lineage_dialects()
    scores = _heuristic_scores(sql_text)
    best = max(scores.values()) if scores else 0
    if best == 0:
        return DialectDetection(
            dialect=default, auto_detected=True, method="default", default=default, scores=scores
        )
    winners = [candidate for candidate in DETECT_CANDIDATES if scores.get(candidate, 0) == best]
    if len(winners) == 1:
        return DialectDetection(
            dialect=winners[0],
            auto_detected=True,
            method="heuristic",
            default=default,
            scores=scores,
        )
    for candidate in winners:  # winners 已按候选优先级排序
        if _parses_cleanly(sql_text, candidate):
            return DialectDetection(
                dialect=candidate,
                auto_detected=True,
                method="trial_parse",
                default=default,
                scores=scores,
            )
    return DialectDetection(
        dialect=default, auto_detected=True, method="default", default=default, scores=scores
    )


def resolve_dialect(requested: str, sql_text: str, *, default: str) -> DialectDetection:
    """解析请求方言:``"auto"`` 哨兵触发识别,否则原样(小写)透传。

    显式方言永远优先——非 ``"auto"`` 一律 explicit 透传,绝不改变既有行为。
    """
    normalized = requested.strip().lower()
    if normalized != AUTO_DIALECT:
        return DialectDetection(
            dialect=normalized,
            auto_detected=False,
            method="explicit",
            default=default,
        )
    return detect_dialect(sql_text, default=default)


__all__ = [
    "AUTO_DIALECT",
    "DETECT_CANDIDATES",
    "DialectDetection",
    "detect_dialect",
    "resolve_dialect",
]
