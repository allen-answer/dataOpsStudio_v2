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
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from app.domain.lineage.dialects import normalize_lineage_dialect, register_lineage_dialects
from app.domain.lineage.parser import LineageSchema
from app.domain.lineage.variables import normalize_template_variables

# 单次 DDL 文本最多处理的语句数(超出部分计入 skipped,不静默丢弃)。
# API 层另有字符数上限,这里是二道闸:防超长文本把解析时间拖爆。
DDL_MAX_STATEMENTS = 2000

# 条目里可能出现的标识符:裸标识符 / 双引号 / 反引号 / T-SQL 方括号。
_IDENT = r"""(?:"[^"]*"|`[^`]*`|\[[^\]]*\]|[A-Za-z_][A-Za-z_0-9$#]*)"""

# 表级约束条目的**形状**匹配(不是首词匹配)。
#
# ★ 为什么是形状而不是关键字集合:``period`` / ``key`` / ``index`` / ``cluster`` /
#   ``partition`` 在 PG / Oracle / DM 里都是非保留字,可以不加引号直接当列名
#   (``period`` 正是本项目 kgrp 报表层的分区列名)。按首词匹配会把整列吞掉。
#   实测 sqlglot 对 ``period text`` / ``key varchar(20)`` / ``cluster int`` 三个
#   方言全都解析正常 —— 我们的过滤是唯一的杀手。
#
# 所以只匹配"关键字 + 左括号"或"关键字 + 名称 + 左括号"这类多词形态:
# ``period text`` 这种单词加类型的条目永远匹配不上,自然不会被误删。
#
# 必须命中的三类(实测,见 PR 说明):
#   1. ``NOT CLUSTER PRIMARY KEY(...)`` —— 进了 sqlglot 会指数回溯到**挂死**
#      (不是变慢,是不返回)。``NOT`` 是所有方言的保留字,不可能是列名。
#   2. ``CLUSTER PRIMARY KEY(...)`` / ``KEY idx(...)`` / ``INDEX idx(...)`` ——
#      不挂死,但 sqlglot 会把关键字本身当成列名,凭空多出 ``CLUSTER`` /
#      ``KEY`` / ``INDEX`` 幻影列。
#   3. 其余标准约束 —— sqlglot 自己就能正确排除,这里拦下只是省解析器功夫。
_CONSTRAINT_SHAPE_RE = re.compile(
    r"^(?:"
    r"NOT\s"
    r"|CONSTRAINT\s+" + _IDENT + r"\s"
    r"|(?:CLUSTER|CLUSTERED|NONCLUSTERED)\s+(?:PRIMARY|UNIQUE)\s*"
    r"|PRIMARY\s+KEY\s*\("
    r"|FOREIGN\s+KEY\s*\("
    r"|UNIQUE\s*(?:(?:KEY|INDEX)\s+)?(?:" + _IDENT + r"\s*)?\("
    r"|CHECK\s*\("
    r"|EXCLUDE\s*(?:USING\s|\()"
    r"|PERIOD\s+FOR\s+" + _IDENT + r"\s*\("
    r"|INHERITS\s*\("
    r"|PARTITION\s+BY\s"
    r"|LIKE\s+" + _IDENT + r"\b"
    r"|(?:FULLTEXT|SPATIAL)\s+(?:KEY|INDEX)\s"
    r"|(?:KEY|INDEX)\s*\("
    r")",
    re.IGNORECASE,
)

# ``KEY idx_b (b)`` / ``INDEX idx_c (c)``(带名字的索引条目)是 **MySQL 独有**的
# 建表内联索引语法,而 MySQL 恰好把 ``KEY`` / ``INDEX`` 列为保留字 —— 两件事合起来
# 给出一个干净的判据:只在 mysql 方言下按索引条目剔除。
# 非 mysql 方言里这套语法根本不合法,出现裸 ``key`` / ``index`` 就是货真价实的列名
# (实测 dm / postgres 下 ``key varchar(20)`` 解析正常),放它过去。
_MYSQL_INDEX_SHAPE_RE = re.compile(
    r"^(?:KEY|INDEX)\s+" + _IDENT + r"\s*\(",
    re.IGNORECASE,
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

# 引号 / 括号标识符的配对表(``[`` 为 T-SQL 方括号标识符)。
_QUOTE_PAIRS = {"'": "'", '"': '"', "`": "`", "[": "]"}

# PG 美元引号的开闭标记:``$$`` 或 ``$tag$``。
_DOLLAR_TAG_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$")

# T-SQL 批处理分隔符:整行只有一个 ``GO``(可带行注释)。SSMS「Generate Scripts」
# 原样导出的脚本用它分批,不认就会丢掉首个 GO 之后的全部 CREATE TABLE。
_GO_RE = re.compile(r"GO[ \t]*(?:--[^\n]*)?(?=\n|\r|$)", re.IGNORECASE)


@dataclass(frozen=True)
class _ScanRules:
    """词法扫描的方言开关 —— 同一段文本在不同方言下的"非代码"边界并不一样。"""

    # MySQL 默认在 '...' / "..." 里认反斜杠转义(NO_BACKSLASH_ESCAPES 默认关)。
    backslash_escapes: bool = False
    # PG 只在 E'...' 前缀字符串里认反斜杠转义。
    escape_string_prefix: bool = False
    # PG 美元引号 $$ ... $$ / $tag$ ... $tag$。
    dollar_quotes: bool = False
    # MySQL 的 # 行注释。
    hash_comments: bool = False
    # PG 的块注释可嵌套。
    nested_block_comments: bool = False
    # T-SQL 的 GO 批处理分隔符。
    batch_separator: bool = False


_MYSQL_RULES = _ScanRules(backslash_escapes=True, hash_comments=True)
_POSTGRES_RULES = _ScanRules(
    escape_string_prefix=True, dollar_quotes=True, nested_block_comments=True
)
_TSQL_RULES = _ScanRules(batch_separator=True)
_DEFAULT_RULES = _ScanRules()

_RULES_BY_DIALECT = {
    "mysql": _MYSQL_RULES,
    "postgres": _POSTGRES_RULES,
    "tsql": _TSQL_RULES,
}


def _rules_for(dialect: str) -> _ScanRules:
    return _RULES_BY_DIALECT.get(dialect, _DEFAULT_RULES)

# 无类型信息时的占位,与 metadata_cache 路径 (schema_from_metadata_cache_rows) 一致。
_UNKNOWN_TYPE = "unknown"


@dataclass(frozen=True)
class _ParsedTable:
    """一条 ``CREATE TABLE`` 解析出的表。

    ``failed_entry_count`` 是"这张表有几个列条目没解析动" —— 与整表被跳过是两回事,
    必须分开报告,否则用户看到"0 跳过"却少了几列,完全无从排查。
    """

    schema_name: str
    table_name: str
    columns: dict[str, str]
    failed_entry_count: int


@dataclass(frozen=True)
class DdlSchemaResult:
    """DDL 文本的解析结果 + 诚实的统计(跳过多少一目了然,不静默吞)。"""

    schema: LineageSchema
    table_count: int
    column_count: int
    skipped_statement_count: int
    # 解析成功但个别列条目没解析动的条目总数(整表被跳过之外的独立信号)。
    failed_column_entry_count: int = 0

    def as_summary(self) -> dict[str, int]:
        """落 parse_summary / 批量报告用的扁平摘要(前端直接展示)。"""
        return {
            "table_count": self.table_count,
            "column_count": self.column_count,
            "skipped_statement_count": self.skipped_statement_count,
            "failed_column_entry_count": self.failed_column_entry_count,
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
    rules = _rules_for(normalized_dialect)
    schema: LineageSchema = {}
    table_count = 0
    column_count = 0
    skipped = 0
    statements = _split_statements(ddl_text, rules)
    if len(statements) > DDL_MAX_STATEMENTS:
        skipped += len(statements) - DDL_MAX_STATEMENTS
        statements = statements[:DDL_MAX_STATEMENTS]
    failed_column_entries = 0
    for statement in statements:
        parsed = _table_from_statement(statement, normalized_dialect, rules, default_schema)
        if parsed is None:
            skipped += 1
            continue
        failed_column_entries += parsed.failed_entry_count
        # 大小写只是写法差异,不是两张表:引号大写与裸小写折叠进同一个桶 / 同一条目,
        # 否则同一逻辑表产生重复条目、计数翻倍,下游取哪份列集取决于插入顺序。
        bucket = _case_key(schema, parsed.schema_name)
        tables = schema.setdefault(bucket, {})
        # 同名表重复出现(DDL 里先建后改)按后者覆盖,与"最后一次定义生效"直觉一致。
        table_key = _case_key(tables, parsed.table_name)
        if table_key not in tables:
            table_count += 1
        else:
            column_count -= len(tables[table_key])
        tables[table_key] = parsed.columns
        column_count += len(parsed.columns)
    return DdlSchemaResult(
        schema=schema,
        table_count=table_count,
        column_count=column_count,
        skipped_statement_count=skipped,
        failed_column_entry_count=failed_column_entries,
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


def _case_key(mapping: Mapping[str, Any], name: str) -> str:
    """已有同名(忽略大小写)键时复用它的原始写法,避免 ODS / ods 分裂成两份。

    与 ``parser.py:_case_get`` 是两件事:那个取**值**,这个取**键**(要拿键回去写入)。
    """
    if name in mapping:
        return name
    lowered = name.lower()
    for key in mapping:
        if key.lower() == lowered:
            return key
    return name


def _table_from_statement(
    statement: str,
    dialect: str,
    rules: _ScanRules,
    default_schema: str | None,
) -> _ParsedTable | None:
    """单条语句 → :class:`_ParsedTable`;非建表 / 无法采纳任何列时为 None。"""
    body_text = _strip_leading_noise(statement, rules)
    header = _CREATE_TABLE_RE.match(body_text)
    if header is None:
        return None
    span = _column_list_span(body_text, header.end(), rules)
    if span is None:
        # CTAS(``CREATE TABLE t AS SELECT ...``)没有列清单:类型要靠查询推导,
        # 不是 DDL 数据源的职责,跳过。
        return None
    open_index, close_index = span
    # 表名与左括号之间也可能夹注释(``CREATE TABLE t /* 订单表 */ (...)``),
    # 剥掉再判 token 数,否则整条被当子查询拒掉。
    name_part = _strip_comments(body_text[header.end() : open_index], rules).strip()
    # 表名必须是单个 token(``t AS`` 之类说明括号是子查询而非列清单,跳过)。
    if not name_part or len(_split_top_level(name_part, " ", rules)) != 1:
        return None
    entries: list[str] = []
    for entry in _split_top_level(body_text[open_index + 1 : close_index], ",", rules):
        if _is_constraint_entry(entry, dialect):
            continue
        # ★ 必须剥注释再重组:重组用 ", " 拼接会丢掉终结行注释的换行,随后的逗号
        # 与右括号被 ``--`` 吞掉,sqlglot 解析失败 → 整张表静默丢弃。
        # DIDA 导出的 SQL 每个字段都带中文行注释,这一形态在真实输入里极其常见。
        cleaned = _strip_comments(entry, rules).strip()
        if cleaned:
            entries.append(cleaned)
    if not entries:
        return None
    table = _table_ref(name_part, dialect)
    if table is None:
        return None
    columns, failed_entry_count = _columns_from_entries(entries, name_part, dialect)
    if not columns:
        return None
    return _ParsedTable(
        schema_name=table.db or default_schema or "",
        table_name=table.name,
        columns=columns,
        failed_entry_count=failed_entry_count,
    )


def _columns_from_entries(
    entries: list[str],
    name_part: str,
    dialect: str,
) -> tuple[dict[str, str], int]:
    """列清单 → ``({列名: 类型}, 解析失败的条目数)``。

    先整表一次解析(快路,绝大多数输入走这条);失败再逐条目解析,**单个条目失败
    只损失该列,不再丢掉整张表**。Oracle / DM 原样导出的 DDL 里 ``NOT NULL`` 列常以
    ``ENABLE`` 结尾(``id NUMBER NOT NULL ENABLE``),实测该条目让 sqlglot 报
    ParseError —— 修复前整表零列被采纳。
    """
    columns = _parse_column_list(entries, name_part, dialect)
    if columns is not None:
        return columns, 0
    merged: dict[str, str] = {}
    failed = 0
    for entry in entries:
        parsed = _parse_column_list([entry], name_part, dialect)
        if parsed is None:
            failed += 1
            continue
        # 解析成功但没产出 ColumnDef = sqlglot 自己认出这是约束,不算失败。
        merged.update(parsed)
    return merged, failed


def _parse_column_list(
    entries: list[str],
    name_part: str,
    dialect: str,
) -> dict[str, str] | None:
    """把列条目重组成 ``CREATE TABLE`` 交给 sqlglot;解析失败返回 None(区别于 ``{}``)。"""
    # 列清单括号之后一律丢弃(TABLESPACE / STORAGE / PARTITION BY / ENGINE= …)。
    # ${VAR} 归一成 :VAR 再进 sqlglot —— 与 sql_text 路径(parser.py:232/250)同一步。
    # 模板化 ETL 导出的 CREATE TABLE ${ODS_SCHEMA}.T (...) 不做这步会解析失败,
    # 而同样的占位符贴进 SQL 框却能正常处理。
    rebuilt = normalize_template_variables(f"CREATE TABLE {name_part} ({', '.join(entries)})")
    try:
        parsed = sqlglot.parse_one(rebuilt, read=dialect)
    except (SqlglotError, RecursionError):
        return None
    if not isinstance(parsed, exp.Create) or not isinstance(parsed.this, exp.Schema):
        return None
    columns: dict[str, str] = {}
    for column_def in parsed.this.expressions:
        if not isinstance(column_def, exp.ColumnDef) or not column_def.name:
            continue
        columns[column_def.name] = (
            column_def.kind.sql(dialect=dialect) if column_def.kind else _UNKNOWN_TYPE
        )
    return columns


def _table_ref(name_part: str, dialect: str) -> exp.Table | None:
    """``name_part`` → :class:`exp.Table`;解析不动为 None。

    单独解析表名(而不是从列清单那次解析里取),这样列条目全军覆没时仍然知道
    这条语句建的是哪张表。
    """
    try:
        table = exp.to_table(normalize_template_variables(name_part).strip(), dialect=dialect)
    except (SqlglotError, RecursionError, ValueError):
        return None
    return table if isinstance(table, exp.Table) and table.name else None


def _is_constraint_entry(entry: str, dialect: str = "") -> bool:
    """条目是不是表级约束 / 索引子句(而不是列定义)。

    ★ 必须在**剥离注释之后**判断:达梦 / Oracle 导出的约束行常带前置中文注释
    (``-- 主键\\n NOT CLUSTER PRIMARY KEY("ID")``),不剥注释就匹配不上形状,
    条目直接送进 sqlglot —— 正是预过滤本要规避的挂死。
    """
    stripped = _strip_comments(entry, _rules_for(dialect)).strip()
    if not stripped:
        return False
    if _CONSTRAINT_SHAPE_RE.match(stripped) is not None:
        return True
    return dialect == "mysql" and _MYSQL_INDEX_SHAPE_RE.match(stripped) is not None


def _strip_comments(text: str, rules: _ScanRules) -> str:
    """去掉注释,保留字符串 / 引号标识符原样;注释位置留一个空格防止 token 粘连。"""
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        comment_end = _skip_comment(text, index, rules)
        if comment_end is not None:
            out.append(" ")
            index = comment_end
            continue
        atom_end = _skip_atom(text, index, rules)
        if atom_end is not None:
            out.append(text[index:atom_end])
            index = atom_end
            continue
        out.append(text[index])
        index += 1
    return "".join(out)


def _strip_leading_noise(text: str, rules: _ScanRules) -> str:
    """跳过语句前导的空白与注释,让 :data:`_CREATE_TABLE_RE` 能贴到 ``CREATE``。"""
    index = 0
    length = len(text)
    while index < length:
        if text[index].isspace():
            index += 1
            continue
        skipped = _skip_comment(text, index, rules)
        if skipped is None:
            break
        index = skipped
    return text[index:]


def _skip_comment(text: str, index: int, rules: _ScanRules) -> int | None:
    pair = text[index : index + 2]
    if pair == "--" or (rules.hash_comments and text[index] == "#"):
        end = text.find("\n", index)
        return len(text) if end < 0 else end + 1
    if pair == "/*":
        if rules.nested_block_comments:
            return _skip_nested_block_comment(text, index)
        end = text.find("*/", index + 2)
        return len(text) if end < 0 else end + 2
    return None


def _skip_nested_block_comment(text: str, index: int) -> int:
    """PG 的块注释可嵌套(``/* /* */ */``);按深度配对,别在第一个 ``*/`` 就收工。"""
    depth = 0
    cursor = index
    length = len(text)
    while cursor < length - 1:
        pair = text[cursor : cursor + 2]
        if pair == "/*":
            depth += 1
            cursor += 2
            continue
        if pair == "*/":
            depth -= 1
            cursor += 2
            if depth <= 0:
                return cursor
            continue
        cursor += 1
    return length


def _skip_atom(text: str, index: int, rules: _ScanRules) -> int | None:
    """index 处若是注释或引号标识符/字面量,返回其结束后的下标;否则 None。

    这是全模块唯一的"跳过非代码"逻辑:切语句、切条目、找括号三处共用,保证
    ``';'`` / ``','`` / ``'('`` 出现在字符串或引号标识符里时不会被误当分隔符。
    """
    comment_end = _skip_comment(text, index, rules)
    if comment_end is not None:
        return comment_end
    if rules.dollar_quotes:
        dollar_end = _skip_dollar_quoted(text, index)
        if dollar_end is not None:
            return dollar_end
    char = text[index]
    # PG 只在 E'...' 里认反斜杠转义;E 本身不是引号,跳过它让引号逻辑接手。
    if rules.escape_string_prefix and char in "eE" and text[index + 1 : index + 2] == "'":
        return _skip_quoted(text, index + 1, "'", backslash=True)
    closing = _QUOTE_PAIRS.get(char)
    if closing is None:
        return None
    # 反斜杠转义只作用于字符串 / 双引号,不作用于反引号与 T-SQL 方括号。
    return _skip_quoted(text, index, closing, backslash=rules.backslash_escapes and char in "'\"")


def _skip_dollar_quoted(text: str, index: int) -> int | None:
    """PG 美元引号 ``$$ ... $$`` / ``$tag$ ... $tag$``。

    不认这个,pg_dump 的 ``CREATE FUNCTION ... AS $$ BEGIN ... END; $$`` 会被按
    函数体内的分号切开,函数里建的临时表被当成真表注入 schema —— 血缘对着虚构列
    产出结果,而且写进缓存。
    """
    match = _DOLLAR_TAG_RE.match(text, index)
    if match is None:
        return None
    tag = match.group(0)
    end = text.find(tag, match.end())
    return len(text) if end < 0 else end + len(tag)


def _skip_quoted(text: str, index: int, closing: str, *, backslash: bool) -> int:
    cursor = index + 1
    length = len(text)
    while cursor < length:
        char = text[cursor]
        if backslash and char == "\\":
            # mysqldump 默认输出 INSERT ... VALUES (1,'it\'s here');不认这个转义,
            # 字符串扫描提前结束,其后引号开启失控字面量,吞掉后续全部 CREATE TABLE。
            cursor += 2
            continue
        if char == closing:
            if text[cursor : cursor + 2] == closing * 2:  # 双写转义:'' / "" / ]]
                cursor += 2
                continue
            return cursor + 1
        cursor += 1
    return length


def _split_top_level(text: str, separator: str, rules: _ScanRules) -> list[str]:
    """按 ``separator`` 在括号深度 0 处切分,跳过注释与引号内容;丢空片段。"""
    segments: list[str] = []
    depth = 0
    start = 0
    index = 0
    length = len(text)
    while index < length:
        skipped = _skip_atom(text, index, rules)
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


def _split_statements(text: str, rules: _ScanRules) -> list[str]:
    """把 DDL 文本切成语句:顶层 ``;``,外加 T-SQL 的整行 ``GO`` 批处理分隔符。

    只按分号切会丢掉 SSMS 导出脚本里首个 ``GO`` 之后的全部建表语句 —— 而且无分号
    时是**静默**丢弃(skipped 报 0,界面显示一切正常),比报错更难排查。
    """
    segments: list[str] = []
    depth = 0
    start = 0
    index = 0
    length = len(text)
    while index < length:
        skipped = _skip_atom(text, index, rules)
        if skipped is not None:
            index = skipped
            continue
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == ";" and depth == 0:
            segments.append(text[start:index])
            start = index + 1
        elif rules.batch_separator and depth == 0 and char in "gG":
            end = _batch_separator_end(text, index)
            if end is not None:
                segments.append(text[start:index])
                start = end
                index = end
                continue
        index += 1
    segments.append(text[start:])
    return [segment for segment in segments if segment.strip()]


def _batch_separator_end(text: str, index: int) -> int | None:
    """``index`` 处若是独占一行的 ``GO``,返回它结束后的下标;否则 None。"""
    match = _GO_RE.match(text, index)
    if match is None:
        return None
    # 必须是行首(前面只有空白),否则 ``go`` 只是某个标识符的一部分。
    line_start = text.rfind("\n", 0, index) + 1
    if text[line_start:index].strip():
        return None
    return match.end()


def _column_list_span(text: str, start: int, rules: _ScanRules) -> tuple[int, int] | None:
    """从 ``start`` 起找第一对顶层括号,返回 ``(开括号下标, 闭括号下标)``。"""
    depth = 0
    open_index: int | None = None
    index = start
    length = len(text)
    while index < length:
        skipped = _skip_atom(text, index, rules)
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
