"""DDL 文本数据源:把 ``CREATE TABLE`` 文本解析成 :data:`LineageSchema`。

对标 DataGrip 的 "DDL data source" —— 用户贴一段建表 DDL,不连真实数据库也能拿到
**列级**血缘。没有 DDL 时行为完全不变(引用表缺元数据 → 维持现状的表级降级)。

设计边界:

- **纯 sqlglot,零数据库驱动**(红线 R1):本模块只做文本 → AST → 类型字典。
- **列清单先做词法预过滤,再交给 sqlglot**。两个实测原因,不是洁癖:

  1. 达梦 / Oracle 导出的表级约束 ``NOT CLUSTER PRIMARY KEY("ID")`` 会让 sqlglot
     的 Oracle 系解析器指数回溯 —— 实测 **挂死**(单条 100s 不返回)。约束条目
     对列类型毫无价值,必须在进解析器之前就剔掉。
  2. 建表语句尾部的物理子句(``TABLESPACE`` / ``STORAGE(...)`` / ``PARTITION BY``
     / ``SEGMENT CREATION`` / ``ENGINE=`` / ``ON [PRIMARY]``)会让 sqlglot 整条
     降级成 ``Command``,整张表的列全部丢失。列清单括号之后一律截断即可。

  预过滤只做"切条目 + 丢约束",列名与类型仍然由 sqlglot 解析(不自己写类型语法)。
- 解析不动的条目 / 语句一律**跳过并计数**,绝不抛异常打断整批(垃圾输入不炸)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from app.domain.lineage.dialects import normalize_lineage_dialect, register_lineage_dialects
from app.domain.lineage.parser import LineageSchema

# 单次 DDL 文本最多处理的语句数(超出部分计入 skipped,不静默丢弃)。
# API 层另有字符数上限,这里是二道闸:防超长文本把解析时间拖爆。
DDL_MAX_STATEMENTS = 2000

# 列清单里以这些关键字开头的条目是表级约束 / 索引 / 分区子句,不是列定义。
# ★ ``not`` / ``cluster`` 必须在列表里:达梦导出的 ``NOT CLUSTER PRIMARY KEY(...)``
#   会让 sqlglot 挂死(见模块 docstring),绝不能让它进解析器。
# 裸标识符恒不区分大小写;真要拿这些词当列名的库会加引号,加了引号就不会命中。
_CONSTRAINT_ENTRY_KEYWORDS = frozenset(
    {
        "check",
        "cluster",
        "clustered",
        "constraint",
        "exclude",
        "foreign",
        "fulltext",
        "index",
        "inherits",
        "key",
        "like",
        "nonclustered",
        "not",
        "partition",
        "period",
        "primary",
        "spatial",
        "unique",
        "using",
        "with",
    }
)

# 建表语句头(到表名为止)。覆盖 OR REPLACE / 临时表 / 外部表 / 达梦 HUGE 表 /
# IF NOT EXISTS 等修饰词;匹配结束处即表名起点。
_CREATE_TABLE_RE = re.compile(
    r"^\s*CREATE\s+"
    r"(?:OR\s+REPLACE\s+)?"
    r"(?:(?:GLOBAL|LOCAL|PRIVATE)\s+)?"
    r"(?:(?:TEMP|TEMPORARY|EXTERNAL|UNLOGGED|MEMORY|HUGE|COLUMN|ROW)\s+)*"
    r"TABLE\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?",
    re.IGNORECASE,
)

# 条目首个裸标识符(用来判断是不是约束条目)。
_LEADING_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9$#]*")

# 引号 / 括号标识符的配对表(``[`` 为 T-SQL 方括号标识符)。
_QUOTE_PAIRS = {"'": "'", '"': '"', "`": "`", "[": "]"}

# 无类型信息时的占位,与 metadata_cache 路径 (schema_from_metadata_cache_rows) 一致。
_UNKNOWN_TYPE = "unknown"


@dataclass(frozen=True)
class DdlSchemaResult:
    """DDL 文本的解析结果 + 诚实的统计(跳过多少一目了然,不静默吞)。"""

    schema: LineageSchema
    table_count: int
    column_count: int
    skipped_statement_count: int

    def as_summary(self) -> dict[str, int]:
        """落 parse_summary / 批量报告用的扁平摘要(前端直接展示)。"""
        return {
            "table_count": self.table_count,
            "column_count": self.column_count,
            "skipped_statement_count": self.skipped_statement_count,
        }


def schema_from_ddl_text(
    ddl_text: str,
    *,
    dialect: str,
    default_schema: str | None = None,
) -> DdlSchemaResult:
    """把一段建表 DDL 文本解析成 :data:`LineageSchema`。

    :param ddl_text: 建表 DDL 原文,可含多条语句 / 注释 / 非建表语句(索引、注释、
        授权、DML 一律跳过)。
    :param dialect: 方言名,按 :func:`normalize_lineage_dialect` 归一
        (``dameng`` → ``dm``,``postgresql`` → ``postgres``)。
    :param default_schema: 裸表名(无 schema 限定)归属的 schema;为空时归入 ``""``
        桶 —— parser 侧对空 schema 名是"扫所有桶"语义,能正常匹配。
    """
    register_lineage_dialects()
    normalized_dialect = normalize_lineage_dialect(dialect)
    schema: LineageSchema = {}
    table_count = 0
    column_count = 0
    skipped = 0
    statements = _split_top_level(ddl_text, ";")
    if len(statements) > DDL_MAX_STATEMENTS:
        skipped += len(statements) - DDL_MAX_STATEMENTS
        statements = statements[:DDL_MAX_STATEMENTS]
    for statement in statements:
        parsed = _table_from_statement(statement, normalized_dialect, default_schema)
        if parsed is None:
            skipped += 1
            continue
        schema_name, table_name, columns = parsed
        # 同名表重复出现(DDL 里先建后改)按后者覆盖,与"最后一次定义生效"直觉一致。
        tables = schema.setdefault(schema_name, {})
        if table_name not in tables:
            table_count += 1
        else:
            column_count -= len(tables[table_name])
        tables[table_name] = columns
        column_count += len(columns)
    return DdlSchemaResult(
        schema=schema,
        table_count=table_count,
        column_count=column_count,
        skipped_statement_count=skipped,
    )


def merge_ddl_schema(base: LineageSchema, ddl: LineageSchema) -> LineageSchema:
    """把 DDL 解析出的表**补进**已有元数据 schema —— 真实元数据永远优先。

    按表粒度合并(不混列):某张表只要元数据缓存里有,就整张用缓存的,DDL 不参与。
    这保证"有真实数据源时行为完全不变",DDL 只填缓存缺失的空洞。

    ★ 未限定 schema 的 DDL 表(落 ``""`` 桶)要**跨全部缓存桶**比对表名,不能只看
    ``""`` 桶。否则 ``""``.T 会与缓存里的 ``ODS``.T 并存,qualify 反而挑中信息更少的
    DDL 副本 —— 血缘比"根本不给 DDL"还差(见 F1 回归用例)。
    """
    merged: LineageSchema = {schema_name: dict(tables) for schema_name, tables in base.items()}
    lowered_index = {
        schema_name.lower(): {table_name.lower() for table_name in tables}
        for schema_name, tables in base.items()
    }
    # 全库表名(小写)集合:裸表名 DDL 判"缓存里到底有没有这张表"用。
    cached_table_names = {name for names in lowered_index.values() for name in names}
    for schema_name, tables in ddl.items():
        existing = lowered_index.get(schema_name.lower(), set())
        target_key = _case_key(merged, schema_name)
        for table_name, columns in tables.items():
            lowered_table = table_name.lower()
            if lowered_table in existing:
                continue
            if not schema_name and lowered_table in cached_table_names:
                # 缓存优先:缓存里已有同名表(在别的 schema 桶里),不再造 "" 桶副本。
                continue
            merged.setdefault(target_key, {})[table_name] = dict(columns)
    return merged


def _case_key(schema: LineageSchema, schema_name: str) -> str:
    """已有同名(忽略大小写)桶时复用它的原始键,避免 ODS / ods 分裂成两个桶。"""
    if schema_name in schema:
        return schema_name
    lowered = schema_name.lower()
    for key in schema:
        if key.lower() == lowered:
            return key
    return schema_name


def _table_from_statement(
    statement: str,
    dialect: str,
    default_schema: str | None,
) -> tuple[str, str, dict[str, str]] | None:
    """单条语句 → ``(schema_name, table_name, {column: type})``;非建表 / 解析失败为 None。"""
    body_text = _strip_leading_noise(statement)
    header = _CREATE_TABLE_RE.match(body_text)
    if header is None:
        return None
    span = _column_list_span(body_text, header.end())
    if span is None:
        # CTAS(``CREATE TABLE t AS SELECT ...``)没有列清单:类型要靠查询推导,
        # 不是 DDL 数据源的职责,跳过。
        return None
    open_index, close_index = span
    name_part = body_text[header.end() : open_index].strip()
    # 表名必须是单个 token(``t AS`` 之类说明括号是子查询而非列清单,跳过)。
    if not name_part or len(_split_top_level(name_part, " ")) != 1:
        return None
    entries = [
        entry
        for entry in _split_top_level(body_text[open_index + 1 : close_index], ",")
        if not _is_constraint_entry(entry)
    ]
    if not entries:
        return None
    # 列清单括号之后一律丢弃(TABLESPACE / STORAGE / PARTITION BY / ENGINE= …)。
    rebuilt = f"CREATE TABLE {name_part} ({', '.join(entry.strip() for entry in entries)})"
    try:
        parsed = sqlglot.parse_one(rebuilt, read=dialect)
    except (SqlglotError, RecursionError):
        return None
    if not isinstance(parsed, exp.Create) or not isinstance(parsed.this, exp.Schema):
        return None
    table = parsed.this.this
    if not isinstance(table, exp.Table) or not table.name:
        return None
    columns: dict[str, str] = {}
    for column_def in parsed.this.expressions:
        if not isinstance(column_def, exp.ColumnDef) or not column_def.name:
            continue
        columns[column_def.name] = (
            column_def.kind.sql(dialect=dialect) if column_def.kind else _UNKNOWN_TYPE
        )
    if not columns:
        return None
    return table.db or default_schema or "", table.name, columns


def _is_constraint_entry(entry: str) -> bool:
    match = _LEADING_WORD_RE.match(entry.strip())
    return match is not None and match.group(0).lower() in _CONSTRAINT_ENTRY_KEYWORDS


def _strip_leading_noise(text: str) -> str:
    """跳过语句前导的空白与注释,让 :data:`_CREATE_TABLE_RE` 能贴到 ``CREATE``。"""
    index = 0
    length = len(text)
    while index < length:
        if text[index].isspace():
            index += 1
            continue
        skipped = _skip_comment(text, index)
        if skipped is None:
            break
        index = skipped
    return text[index:]


def _skip_comment(text: str, index: int) -> int | None:
    pair = text[index : index + 2]
    if pair == "--":
        end = text.find("\n", index)
        return len(text) if end < 0 else end + 1
    if pair == "/*":
        end = text.find("*/", index + 2)
        return len(text) if end < 0 else end + 2
    return None


def _skip_atom(text: str, index: int) -> int | None:
    """index 处若是注释或引号标识符/字面量,返回其结束后的下标;否则 None。

    这是全模块唯一的"跳过非代码"逻辑:切语句、切条目、找括号三处共用,保证
    ``';'`` / ``','`` / ``'('`` 出现在字符串或引号标识符里时不会被误当分隔符。
    """
    comment_end = _skip_comment(text, index)
    if comment_end is not None:
        return comment_end
    char = text[index]
    closing = _QUOTE_PAIRS.get(char)
    if closing is None:
        return None
    cursor = index + 1
    length = len(text)
    while cursor < length:
        if text[cursor] == closing:
            if text[cursor : cursor + 2] == closing * 2:  # 双写转义:'' / "" / ]]
                cursor += 2
                continue
            return cursor + 1
        cursor += 1
    return length


def _split_top_level(text: str, separator: str) -> list[str]:
    """按 ``separator`` 在括号深度 0 处切分,跳过注释与引号内容;丢空片段。"""
    segments: list[str] = []
    depth = 0
    start = 0
    index = 0
    length = len(text)
    while index < length:
        skipped = _skip_atom(text, index)
        if skipped is not None:
            index = skipped
            continue
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == separator and depth == 0:
            segments.append(text[start:index])
            start = index + 1
        index += 1
    segments.append(text[start:])
    return [segment for segment in segments if segment.strip()]


def _column_list_span(text: str, start: int) -> tuple[int, int] | None:
    """从 ``start`` 起找第一对顶层括号,返回 ``(开括号下标, 闭括号下标)``。"""
    depth = 0
    open_index: int | None = None
    index = start
    length = len(text)
    while index < length:
        skipped = _skip_atom(text, index)
        if skipped is not None:
            index = skipped
            continue
        char = text[index]
        if char == "(":
            if depth == 0:
                open_index = index
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return None
            if depth == 0 and open_index is not None:
                return open_index, index
        index += 1
    return None


__all__ = [
    "DDL_MAX_STATEMENTS",
    "DdlSchemaResult",
    "merge_ddl_schema",
    "schema_from_ddl_text",
]
