"""批量血缘完整导出 exporter(L-5+ PR1,A 组 7 sheet)。

纯函数式:只吃批量 report dict(worker 落的 ``lineage_batch_report.json`` +
L-4 ``semantic_view``),产出 ``write_xlsx_workbook`` 可直接消费的
``list[(sheet_name, list[Column], list[Row])]``。

R1:本模块只依赖 ``app.domain.schema``,**禁 import 任何数据库驱动**——不连库、
不查子图,数据面完全来自入参 report。
R5:字符串单元格入表前经 :func:`_safe_text` 去控制字符 + 超长截断(防脱敏
processor 绕过 / Excel 单元格畸形);公式注入前缀防护由 ``write_xlsx_workbook`` 内置。

范围(PR1 = A 组,零 worker 改动,数据今天就在 report JSON 里):
    流程总览 / 脚本清单 / 跨脚本依赖 / 风险提示 / 流程图分组 / 表角色 / 目标整合(刷新模式)。
明细类 7 sheet(过程段/语句/解析失败明细/动态 SQL/脚本警告/字段映射/表级数据流)
依赖 worker 解除 ``pop("_report")``,留 PR2。
"""

from __future__ import annotations

from typing import Any

from app.domain.schema import Column, Row

# Excel 单元格硬上限 32767 字符;留余量放截断标记。
_MAX_CELL_CHARS = 32000
_TRUNCATED_SUFFIX = "…(truncated)"

# 目标整合 sheet 展开的写操作计数列(与 semantic TargetIntegration.counts 键对齐)。
_COUNT_KINDS = ("insert", "update", "merge", "delete", "truncate")

# A 组 7 sheet 名(与设计稿 §一 1.x parity + L-4 增值对齐)。
SHEET_OVERVIEW = "流程总览"
SHEET_SCRIPTS = "脚本清单"
SHEET_SCRIPT_EDGES = "跨脚本依赖"
SHEET_RISKS = "风险提示"
SHEET_FLOW_GROUPS = "流程图分组"
SHEET_TABLE_ROLES = "表角色"
SHEET_TARGET_INTEGRATION = "目标整合"


def batch_export_sheets(
    report: dict[str, Any] | None,
) -> list[tuple[str, list[Column], list[Row]]]:
    """批量 report → A 组 7 sheet。

    report 为空 / 缺 ``semantic_view`` 时,对应 sheet 守空(仅表头,零数据行),
    始终返回 7 个 sheet,保证下游 workbook 结构稳定。
    """
    data = report or {}
    semantic = data.get("semantic_view") or {}
    return [
        _overview_sheet(data),
        _scripts_sheet(data),
        _script_edges_sheet(data),
        _risks_sheet(semantic),
        _flow_groups_sheet(semantic),
        _table_roles_sheet(semantic),
        _target_integration_sheet(semantic),
    ]


# ---------------------------------------------------------------------------
# 单 sheet 构建
# ---------------------------------------------------------------------------


def _overview_sheet(report: dict[str, Any]) -> tuple[str, list[Column], list[Row]]:
    """流程总览:report 顶层统计单行汇总。"""
    skipped = report.get("skipped") or {}
    columns = _columns(
        "file_count",
        "parsed",
        "failed",
        "skipped_non_sql",
        "skipped_too_large",
        "skipped_over_file_limit",
        "table_edge_total",
        "column_mapping_total",
        "script_edge_count",
    )
    row = Row(
        values=[
            _int(report.get("file_count")),
            _int(report.get("parsed")),
            _int(report.get("failed")),
            _int(skipped.get("non_sql")),
            _int(skipped.get("too_large")),
            _int(skipped.get("over_file_limit")),
            _int(report.get("table_edge_total")),
            _int(report.get("column_mapping_total")),
            len(report.get("script_edges") or []),
        ]
    )
    return (SHEET_OVERVIEW, columns, [row])


def _scripts_sheet(report: dict[str, Any]) -> tuple[str, list[Column], list[Row]]:
    """脚本清单:每文件一行(报告项 files[])。"""
    columns = _columns(
        "file_name",
        "status",
        "run_id",
        "table_edge_count",
        "column_mapping_count",
        "parse_error_count",
        "lenient_statement_count",
        "tables_read",
        "tables_written",
        "error",
    )
    rows: list[Row] = []
    for raw in report.get("files") or []:
        item = raw or {}
        rows.append(
            Row(
                values=[
                    _text(item.get("source_ref")),
                    _text(item.get("status")),
                    _text(item.get("run_id")),
                    _int(item.get("table_edge_count")),
                    _int(item.get("column_mapping_count")),
                    _int(item.get("parse_error_count")),
                    _int(item.get("lenient_statement_count")),
                    _join(item.get("tables_read")),
                    _join(item.get("tables_written")),
                    _text(item.get("error")),
                ]
            )
        )
    return (SHEET_SCRIPTS, columns, rows)


def _script_edges_sheet(report: dict[str, Any]) -> tuple[str, list[Column], list[Row]]:
    """跨脚本依赖:文件 A 写 ∩ 文件 B 读(script_edges[])。"""
    columns = _columns("producer_file", "consumer_file", "table")
    rows: list[Row] = []
    for raw in report.get("script_edges") or []:
        edge = raw or {}
        # worker _lineage_batch_script_edges 落的键是 source_file/target_file/tables
        # (LineageBatchScriptEdge 契约),tables 是 list —— 之前用 producer/consumer/table
        # 取不到值,真跑时本 sheet 全空。
        rows.append(
            Row(
                values=[
                    _text(edge.get("source_file")),
                    _text(edge.get("target_file")),
                    _join(edge.get("tables")),
                ]
            )
        )
    return (SHEET_SCRIPT_EDGES, columns, rows)


def _risks_sheet(semantic: dict[str, Any]) -> tuple[str, list[Column], list[Row]]:
    """风险提示:L-4 分级风险(risks[])+ 人话观察(observations[],type=observation)。"""
    columns = _columns("level", "type", "message")
    rows: list[Row] = []
    for raw in semantic.get("risks") or []:
        risk = raw or {}
        rows.append(
            Row(
                values=[
                    _text(risk.get("level")),
                    _text(risk.get("type")),
                    _text(risk.get("message")),
                ]
            )
        )
    for observation in semantic.get("observations") or []:
        rows.append(Row(values=[None, _text("observation"), _text(observation)]))
    return (SHEET_RISKS, columns, rows)


def _flow_groups_sheet(semantic: dict[str, Any]) -> tuple[str, list[Column], list[Row]]:
    """流程图分组:按目标表聚合(targets[] 派生源表 / 源脚本)。"""
    columns = _columns("target_table", "source_tables", "source_files")
    rows: list[Row] = []
    for raw in semantic.get("targets") or []:
        target = raw or {}
        rows.append(
            Row(
                values=[
                    _text(target.get("table")),
                    _join(target.get("source_tables")),
                    _join(target.get("source_files")),
                ]
            )
        )
    return (SHEET_FLOW_GROUPS, columns, rows)


def _table_roles_sheet(semantic: dict[str, Any]) -> tuple[str, list[Column], list[Row]]:
    """表角色:结构角色 + 命名角色(table_roles[],L-4 增值 sheet)。"""
    columns = _columns("table", "primary_role", "roles")
    rows: list[Row] = []
    for raw in semantic.get("table_roles") or []:
        role = raw or {}
        rows.append(
            Row(
                values=[
                    _text(role.get("table")),
                    _text(role.get("primary_role")),
                    _join(role.get("roles")),
                ]
            )
        )
    return (SHEET_TABLE_ROLES, columns, rows)


def _target_integration_sheet(
    semantic: dict[str, Any],
) -> tuple[str, list[Column], list[Row]]:
    """目标整合(刷新模式):每目标表跨脚本整合视图(targets[],L-4 增值 sheet)。"""
    columns = _columns(
        "table",
        "primary_role",
        "roles",
        "refresh_mode",
        *(f"count_{kind}" for kind in _COUNT_KINDS),
        "source_tables",
        "source_files",
        "titles",
    )
    rows: list[Row] = []
    for raw in semantic.get("targets") or []:
        target = raw or {}
        counts = target.get("counts") or {}
        rows.append(
            Row(
                values=[
                    _text(target.get("table")),
                    _text(target.get("primary_role")),
                    _join(target.get("roles")),
                    _text(target.get("refresh_mode")),
                    *(_int(counts.get(kind)) for kind in _COUNT_KINDS),
                    _join(target.get("source_tables")),
                    _join(target.get("source_files")),
                    _join(target.get("titles")),
                ]
            )
        )
    return (SHEET_TARGET_INTEGRATION, columns, rows)


# ---------------------------------------------------------------------------
# 单元格 helper
# ---------------------------------------------------------------------------


def _columns(*names: str) -> list[Column]:
    return [Column(name=name) for name in names]


def _int(value: Any) -> int:
    """计数列:None / 非数一律归 0(与 worker 汇总口径一致)。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _text(value: Any) -> str | None:
    """字符串单元格:None 落空格,其余经 R5 清洗(去控制字符 + 超长截断)。"""
    if value is None:
        return None
    return _safe_text(str(value))


def _join(value: Any, sep: str = "; ") -> str | None:
    """列表列展平成单格;非列表按标量处理。"""
    if value is None:
        return None
    if isinstance(value, list):
        return _safe_text(sep.join(str(item) for item in value))
    return _safe_text(str(value))


def _safe_text(text: str) -> str:
    """R5:剥控制字符(< 0x20,含换行/制表)换空格 + 超 Excel 上限截断。"""
    cleaned = "".join(char if char >= " " else " " for char in text)
    if len(cleaned) > _MAX_CELL_CHARS:
        cleaned = cleaned[: _MAX_CELL_CHARS - len(_TRUNCATED_SUFFIX)] + _TRUNCATED_SUFFIX
    return cleaned
