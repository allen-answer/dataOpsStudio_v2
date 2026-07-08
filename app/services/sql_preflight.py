"""SQL preflight 体检 —— 纯文本启发式 advisory 检查(差距矩阵 C-11)。

1.x `services/sql_preflight.py` 的 2.0 形态:**只提示不拦截**。硬校验仍归
`dbclients/sql_guard`(只读守卫)与库内 EXPLAIN;本模块只做执行前的"体检"提示,
帮用户在跑之前看见"无 WHERE 的全表写""SELECT *""缺 LIMIT"这类风险。

设计约束:
- **纯函数、无副作用、不碰 DB**(R1 友好:services 层禁 import 数据库驱动)。
  只吃 SQL 文本,吐 finding 列表 —— 因此可脱离后端整栈单测。
- 方言无关的粗粒度文本启发式:先剥注释 / 字符串字面量再跑正则,避免把
  字符串里的关键字或 `COUNT(*)` 误判。宁可漏报不误报(advisory 不能扰民)。
- 每条 finding 带稳定 `code`,前端按 code 做 i18n,`message` 作英文兜底。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

# ── 严重级别(advisory 只有这两档,都不拦截执行)──────────────────────────
Severity = Literal["warning", "info"]
SEVERITY_WARNING: Final[Severity] = "warning"  # 有真实误伤面(全表写),值得停一下
SEVERITY_INFO: Final[Severity] = "info"  # 习惯 / 性能提示(SELECT * / 缺 LIMIT)


@dataclass(frozen=True)
class PreflightFinding:
    """一条体检发现。severity 决定前端提示卡的配色,code 决定 i18n 文案。"""

    severity: Severity
    code: str
    message: str


# ── 文本清洗:剥掉会污染关键字匹配的片段 ────────────────────────────────
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"--[^\n]*")
# 单引号字符串(含 '' 转义);把内容抹平成占位,避免字符串里的 WHERE/LIMIT 干扰。
_STRING_LITERAL = re.compile(r"'(?:''|[^'])*'")


def _strip_noise(sql: str) -> str:
    cleaned = _BLOCK_COMMENT.sub(" ", sql)
    cleaned = _LINE_COMMENT.sub(" ", cleaned)
    cleaned = _STRING_LITERAL.sub("''", cleaned)
    return cleaned


# ── 规则正则(全部大小写不敏感,跑在清洗后的文本上)──────────────────────
_LEADING_KEYWORD = re.compile(r"^\s*([A-Za-z]+)", re.IGNORECASE)
_HAS_WHERE = re.compile(r"\bWHERE\b", re.IGNORECASE)
# SELECT 后紧跟 * 或 alias.*(可含 DISTINCT/ALL);COUNT(*) 不在 SELECT 之后紧跟,故不误判。
_SELECT_STAR = re.compile(
    r"\bSELECT\s+(?:DISTINCT\s+|ALL\s+)?(?:[\w\"`]+\s*\.\s*)?\*",
    re.IGNORECASE,
)
# 任一"限行"手段存在即视为已限行(方言并集:LIMIT / TOP n / FETCH FIRST|NEXT / ROWNUM)。
_HAS_ROW_CAP = re.compile(
    r"\bLIMIT\b|\bTOP\s+\d+|\bFETCH\s+(?:FIRST|NEXT)\b|\bROWNUM\b",
    re.IGNORECASE,
)


def _leading_keyword(cleaned_sql: str) -> str:
    match = _LEADING_KEYWORD.match(cleaned_sql)
    return match.group(1).upper() if match else ""


def run_sql_preflight(sql: str) -> list[PreflightFinding]:
    """对单条 SQL 文本跑体检,返回 advisory finding 列表(可能为空)。

    纯函数:同一输入恒定输出,无 I/O。空 / 纯空白输入返回空列表。
    """
    if not sql or not sql.strip():
        return []

    cleaned = _strip_noise(sql)
    keyword = _leading_keyword(cleaned)
    has_where = bool(_HAS_WHERE.search(cleaned))
    findings: list[PreflightFinding] = []

    # 规则 1:UPDATE / DELETE 无 WHERE → 全表写,最高优先提醒。
    if keyword in {"UPDATE", "DELETE"} and not has_where:
        findings.append(
            PreflightFinding(
                severity=SEVERITY_WARNING,
                code="write_without_where",
                message=(f"{keyword} without a WHERE clause affects every row in the table."),
            )
        )

    is_select = keyword in {"SELECT", "WITH"}

    # 规则 2:SELECT * → 建议显式列(稳定性 / 传输量)。
    if is_select and _SELECT_STAR.search(cleaned):
        findings.append(
            PreflightFinding(
                severity=SEVERITY_INFO,
                code="select_star",
                message="SELECT * returns all columns; prefer an explicit column list.",
            )
        )

    # 规则 3:SELECT 缺行数上限 → 大表可能拉回巨量行。
    if is_select and not _HAS_ROW_CAP.search(cleaned):
        findings.append(
            PreflightFinding(
                severity=SEVERITY_INFO,
                code="missing_limit",
                message="Query has no row limit; large tables may return many rows.",
            )
        )

    return findings
