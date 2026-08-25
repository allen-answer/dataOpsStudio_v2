"""DDL 文本数据源(app/domain/lineage/ddl_schema.py)单测。

覆盖:方言 / 注释 / 引号标识符 / 裸表名 + 默认 schema / 垃圾输入不炸 /
达梦导出实拍形态 / 与元数据缓存合并的优先级 / 端到端拿回列级血缘。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.lineage import (
    LineageParseRequest,
    analyze_sql_lineage,
    apply_ddl_schema,
    lineage_ddl_fingerprint,
    lineage_sql_hash,
    merge_ddl_schema,
    schema_from_ddl_text,
)
from app.domain.lineage.ddl_schema import DDL_MAX_STATEMENTS, _is_constraint_entry


def test_parses_basic_create_table_into_schema() -> None:
    result = schema_from_ddl_text(
        "CREATE TABLE ods.orders (id INT, amount DECIMAL(12,2))",
        dialect="mysql",
    )

    assert result.schema == {"ods": {"orders": {"id": "INT", "amount": "DECIMAL(12, 2)"}}}
    assert result.table_count == 1
    assert result.column_count == 2
    assert result.skipped == {}
    assert result.skipped_statement_count == 0
    assert result.failed_column_entry_count == 0


@pytest.mark.parametrize(
    ("dialect", "ddl", "expected_schema", "expected_table"),
    [
        ("dameng", 'CREATE TABLE "ODS"."ORDERS" ("ID" NUMBER(18))', "ODS", "ORDERS"),
        ("oracle", "CREATE TABLE ODS.ORDERS (ID NUMBER(18))", "ODS", "ORDERS"),
        ("mysql", "CREATE TABLE `ods`.`orders` (`id` BIGINT)", "ods", "orders"),
        ("postgresql", "CREATE TABLE ods.orders (id bigint)", "ods", "orders"),
        ("tsql", "CREATE TABLE [dbo].[Orders] ([Id] INT)", "dbo", "Orders"),
    ],
)
def test_parses_each_supported_dialect(
    dialect: str, ddl: str, expected_schema: str, expected_table: str
) -> None:
    """方言名按 normalize_lineage_dialect 归一(dameng→dm、postgresql→postgres)。"""
    result = schema_from_ddl_text(ddl, dialect=dialect)

    assert list(result.schema) == [expected_schema]
    assert list(result.schema[expected_schema]) == [expected_table]


def test_rejects_dialect_outside_whitelist() -> None:
    with pytest.raises(ValueError, match="lineage dialect must be"):
        schema_from_ddl_text("CREATE TABLE t (a INT)", dialect="db2")


def test_bare_table_name_uses_default_schema() -> None:
    result = schema_from_ddl_text(
        "CREATE TABLE orders (id INT)", dialect="mysql", default_schema="ods"
    )

    assert result.schema == {"ods": {"orders": {"id": "INT"}}}


def test_bare_table_name_without_default_schema_lands_in_empty_bucket() -> None:
    """裸表名 + 无默认 schema → 空串桶;parser 侧对空 schema 名是"扫所有桶"语义。"""
    result = schema_from_ddl_text("CREATE TABLE orders (id INT)", dialect="mysql")

    assert result.schema == {"": {"orders": {"id": "INT"}}}


def test_qualified_name_wins_over_default_schema() -> None:
    result = schema_from_ddl_text(
        "CREATE TABLE dwd.orders (id INT)", dialect="mysql", default_schema="ods"
    )

    assert result.schema == {"dwd": {"orders": {"id": "INT"}}}


def test_skips_comments_before_and_inside_create_table() -> None:
    ddl = """
    -- 订单主表
    /* 多行
       注释 */
    CREATE TABLE ods.orders (
      id INT,      -- 主键
      /* 金额 */ amount DECIMAL(12,2)
    );
    """
    result = schema_from_ddl_text(ddl, dialect="mysql")

    assert result.schema == {"ods": {"orders": {"id": "INT", "amount": "DECIMAL(12, 2)"}}}


def test_preserves_quoted_identifier_case_and_spaces() -> None:
    result = schema_from_ddl_text(
        'CREATE TABLE "Ods"."Order Items" ("Item Id" NUMBER(18), "QTY" NUMBER(9))',
        dialect="dm",
    )

    assert result.schema == {"Ods": {"Order Items": {"Item Id": "NUMBER(18)", "QTY": "NUMBER(9)"}}}


def test_separators_inside_string_defaults_do_not_split_statements() -> None:
    """默认值里的 ``;`` / ``,`` / ``(`` 不能被当成语句或列分隔符。"""
    result = schema_from_ddl_text(
        "CREATE TABLE ods.t (a VARCHAR(50) DEFAULT 'x;y,z(1)', b INT)",
        dialect="mysql",
    )

    assert result.schema == {"ods": {"t": {"a": "VARCHAR(50)", "b": "INT"}}}


def test_drops_trailing_physical_clauses() -> None:
    """TABLESPACE / STORAGE / PARTITION BY 会让 sqlglot 整条降级成 Command,必须截断。"""
    ddl = """
    CREATE TABLE ODS.ORDERS (ID NUMBER(18), AMT NUMBER(12,2))
      PCTFREE 10 STORAGE(INITIAL 64K) TABLESPACE TS_MAIN;
    """
    result = schema_from_ddl_text(ddl, dialect="oracle")

    assert result.schema == {"ODS": {"ORDERS": {"ID": "NUMBER(18)", "AMT": "NUMBER(12, 2)"}}}


def test_parses_dm_export_shape_with_not_cluster_primary_key() -> None:
    """达梦导出实拍形态:引号标识符 + NOT CLUSTER PRIMARY KEY + STORAGE 尾巴。

    ★ ``NOT CLUSTER PRIMARY KEY(...)`` 实测会让 sqlglot 的 Oracle 系解析器指数
    回溯到挂死,靠 _is_constraint_entry 在进解析器之前剔除(见下一个用例)。
    """
    ddl = """
    CREATE TABLE "ODS"."ORDERS"
    (
    "ORDER_ID" NUMBER(18) NOT NULL,
    "CUST_ID" VARCHAR2(64),
    "AMT" NUMBER(12,2) DEFAULT 0,
    NOT CLUSTER PRIMARY KEY("ORDER_ID")) STORAGE(ON "MAIN", CLUSTERBTR) ;
    """
    result = schema_from_ddl_text(ddl, dialect="dm")

    assert result.schema == {
        "ODS": {
            "ORDERS": {
                "ORDER_ID": "NUMBER(18)",
                "CUST_ID": "VARCHAR2(64)",
                "AMT": "NUMBER(12, 2)",
            }
        }
    }


@pytest.mark.parametrize(
    "entry",
    [
        'NOT CLUSTER PRIMARY KEY("ORDER_ID")',
        "CLUSTER PRIMARY KEY(ID)",
        "CONSTRAINT pk_orders PRIMARY KEY (id)",
        "PRIMARY KEY (id)",
        "UNIQUE KEY idx_a (a)",
        "UNIQUE (a, b)",
        "FOREIGN KEY (cust_id) REFERENCES ods.cust (id)",
        "CHECK (amt >= 0)",
        "KEY (b)",
        "INDEX (c)",
        "PERIOD FOR system_time (vs, ve)",
        "EXCLUDE USING gist (room WITH =)",
        "PARTITION BY RANGE (dt)",
        "INHERITS (parent)",
        "FULLTEXT KEY ft_a (a)",
        # F4:带前置注释的约束条目 —— 剥注释之后仍然要认出来,否则进 sqlglot 挂死。
        '-- 主键约束\n NOT CLUSTER PRIMARY KEY("ID")',
        "/* 主键 */ PRIMARY KEY (id)",
    ],
)
def test_constraint_entries_are_filtered_before_sqlglot(entry: str) -> None:
    """★ 回归护栏:约束条目一律不进 sqlglot。

    ``NOT CLUSTER PRIMARY KEY`` 若漏过这层过滤会让解析器挂死(不是变慢,是不返回);
    ``CLUSTER PRIMARY KEY`` / ``KEY (...)`` 漏过则凭空多出幻影列。
    """
    assert _is_constraint_entry(entry, "dm") is True


@pytest.mark.parametrize(
    "entry",
    [
        "id INT",
        '"NOT_NULL_FLAG" CHAR(1)',
        "amount DECIMAL(12,2) DEFAULT 0",
        "`key_name` VARCHAR(20)",
        # F3:PG / Oracle / DM 的非保留字,不加引号就能当列名。
        # ``period`` 是本项目 kgrp 报表层的真实分区列名 —— 被吞掉等于整列血缘丢失。
        "period text",
        "period VARCHAR2(8)",
        "key varchar(20)",
        "index numeric(10)",
        "cluster int",
        "partition int",
        "exclude int",
        "constraint_name varchar(64)",
        "primary_flag char(1)",
        "check_sum bigint",
        # 带尾部注释的普通列(达梦 / DIDA 导出每列都带中文注释)。
        "period VARCHAR2(8) -- 账期分区",
        "-- 账期分区\n period VARCHAR2(8)",
    ],
)
def test_column_entries_are_not_filtered(entry: str) -> None:
    assert _is_constraint_entry(entry, "dm") is False


def test_trailing_line_comment_on_last_column_keeps_the_table() -> None:
    """F5:列尾 ``--`` 注释被逗号拼接吞掉右括号 → 整表静默丢弃。"""
    result = schema_from_ddl_text(
        "CREATE TABLE ods.t (a INT, b INT -- 最后一列说明\n);", dialect="dm"
    )

    assert result.schema == {"ods": {"t": {"a": "INT", "b": "INT"}}}
    assert result.skipped_statement_count == 0


def test_leading_comma_style_line_comments_keep_the_table() -> None:
    """F5:前导逗号风格(每行 ``, col -- 注释``)同样中招。"""
    ddl = """CREATE TABLE ods.t (
      a INT -- 首列
    , b INT -- 次列
    , c INT -- 末列
    );"""
    result = schema_from_ddl_text(ddl, dialect="dm")

    assert result.schema == {"ods": {"t": {"a": "INT", "b": "INT", "c": "INT"}}}


def test_comment_between_table_name_and_column_list() -> None:
    result = schema_from_ddl_text("CREATE TABLE ods.t /* 订单表 */ (a INT)", dialect="dm")

    assert result.schema == {"ods": {"t": {"a": "INT"}}}


def test_single_unparsable_entry_only_loses_that_column() -> None:
    """F6:``NOT NULL ENABLE`` 一个条目失败,修复前整表零列被采纳。"""
    result = schema_from_ddl_text(
        "CREATE TABLE ods.t (id NUMBER NOT NULL ENABLE, amt NUMBER(18,2), nm VARCHAR2(64));",
        dialect="oracle",
    )

    assert result.schema == {"ods": {"t": {"amt": "NUMBER(18, 2)", "nm": "VARCHAR2(64)"}}}
    # 整表没被跳过,但"少了一列"必须是独立可见的信号,不能报 0 跳过就完事。
    assert result.skipped_statement_count == 0
    assert result.failed_column_entry_count == 1


@pytest.mark.parametrize("entry", ["KEY idx_b (b)", "INDEX idx_c (c)"])
def test_named_index_entries_are_filtered_only_for_mysql(entry: str) -> None:
    """``KEY idx (col)`` 是 MySQL 独有语法,且 MySQL 里 KEY/INDEX 是保留字。

    非 mysql 方言下这套语法不合法,裸 ``key`` / ``index`` 只可能是列名 —— 放过去。
    """
    assert _is_constraint_entry(entry, "mysql") is True
    assert _is_constraint_entry(entry, "dm") is False
    assert _is_constraint_entry(entry, "postgres") is False


def test_skips_non_create_table_statements_and_counts_them() -> None:
    ddl = """
    CREATE TABLE ods.orders (id INT);
    CREATE INDEX idx_o ON ods.orders (id);
    CREATE VIEW v_orders AS SELECT id FROM ods.orders;
    COMMENT ON COLUMN ods.orders.id IS 'pk';
    GRANT SELECT ON ods.orders TO PUBLIC;
    INSERT INTO ods.orders VALUES (1);
    """
    result = schema_from_ddl_text(ddl, dialect="oracle")

    assert list(result.schema["ods"]) == ["orders"]
    assert result.table_count == 1
    assert result.skipped_statement_count == 5


def test_skips_ctas_without_column_list() -> None:
    """CTAS 没有列清单(类型要靠查询推导),不是 DDL 数据源的职责。"""
    result = schema_from_ddl_text(
        "CREATE TABLE ods.copy AS SELECT id FROM (SELECT id FROM ods.orders) s",
        dialect="mysql",
    )

    assert result.schema == {}
    assert result.skipped_statement_count == 1


@pytest.mark.parametrize(
    "garbage",
    [
        "",
        "   \n\t  ",
        "-- only a comment",
        "/* unterminated",
        "CREATE TABLE",
        "CREATE TABLE (",
        "CREATE TABLE t (",
        "CREATE TABLE t ()",
        "))))",
        "'unterminated string",
        "CREATE TABLE t (,,,)",
        "DROP TABLE t; SELECT 1; ¯\\_(ツ)_/¯",
    ],
)
def test_garbage_input_never_raises(garbage: str) -> None:
    result = schema_from_ddl_text(garbage, dialect="mysql")

    assert result.schema == {}
    assert result.table_count == 0


def test_repeated_table_definition_keeps_last_and_counts_once() -> None:
    ddl = "CREATE TABLE ods.t (a INT); CREATE TABLE ods.t (a INT, b INT);"
    result = schema_from_ddl_text(ddl, dialect="mysql")

    assert result.schema == {"ods": {"t": {"a": "INT", "b": "INT"}}}
    assert result.table_count == 1
    assert result.column_count == 2


def test_statement_budget_counts_overflow_as_skipped() -> None:
    ddl = "".join(f"CREATE TABLE t{index} (a INT);" for index in range(DDL_MAX_STATEMENTS + 5))
    result = schema_from_ddl_text(ddl, dialect="mysql")

    assert result.table_count == DDL_MAX_STATEMENTS
    assert result.skipped_statement_count == 5


def test_entries_without_a_type_are_dropped_not_guessed() -> None:
    """无类型的条目 sqlglot 解成 Identifier 而非 ColumnDef —— 略过,不臆造类型。"""
    result = schema_from_ddl_text("CREATE TABLE ods.t (a INT, b)", dialect="mysql")

    assert result.schema == {"ods": {"t": {"a": "INT"}}}


# ── 与元数据缓存合并 ──────────────────────────────────────────────────


def test_merge_keeps_metadata_cache_table_over_ddl() -> None:
    """有真实元数据的表永远用元数据 —— DDL 只填空洞,行为不回归。"""
    base = {"ods": {"orders": {"id": "integer"}}}
    ddl = {"ods": {"orders": {"id": "INT", "extra": "INT"}, "dwd": {"id": "INT"}}}

    merged = merge_ddl_schema(base, ddl)

    assert merged["ods"]["orders"] == {"id": "integer"}
    assert merged["ods"]["dwd"] == {"id": "INT"}


def test_merge_matches_table_names_case_insensitively() -> None:
    base = {"ODS": {"ORDERS": {"ID": "integer"}}}

    merged = merge_ddl_schema(base, {"ods": {"orders": {"ID": "INT", "EXTRA": "INT"}}})

    assert merged == {"ODS": {"ORDERS": {"ID": "integer"}}}


def test_merge_bare_ddl_table_defers_to_cache_in_another_schema() -> None:
    """F1:裸表名 DDL 不得在 "" 桶造出缓存同名表的副本(否则 qualify 反被遮蔽)。"""
    base = {"ODS": {"SRC": {"ID": "integer", "AMT": "numeric"}}}

    merged = merge_ddl_schema(base, {"": {"SRC": {"ID": "INT"}}})

    assert merged == {"ODS": {"SRC": {"ID": "integer", "AMT": "numeric"}}}


def test_merge_bare_ddl_table_absent_from_cache_still_lands_in_empty_bucket() -> None:
    """F1 的另一半:全库都没有这张表时 DDL 仍要补进来(别为了修 F1 矫枉过正)。"""
    base = {"ODS": {"SRC": {"ID": "integer"}}}

    merged = merge_ddl_schema(base, {"": {"NEW_T": {"ID": "INT"}}})

    assert merged[""] == {"NEW_T": {"ID": "INT"}}
    assert merged["ODS"] == {"SRC": {"ID": "integer"}}


def test_merge_does_not_mutate_inputs() -> None:
    base = {"ods": {"orders": {"id": "integer"}}}
    ddl = {"ods": {"dwd": {"id": "INT"}}}

    merge_ddl_schema(base, ddl)

    assert base == {"ods": {"orders": {"id": "integer"}}}
    assert ddl == {"ods": {"dwd": {"id": "INT"}}}


# ── F13 摘要必须诚实 ──────────────────────────────────────────────────


def test_skipped_reasons_are_distinguishable() -> None:
    """六种结果混成一个数字会把用户引向完全错误的排查方向。"""
    ddl = """
    CREATE INDEX idx_a ON ods.t (a);
    CREATE TABLE ods.ctas AS SELECT * FROM ods.src;
    CREATE TABLE ods.only_constraints (PRIMARY KEY (id));
    GRANT SELECT ON ods.t TO reader;
    """
    result = schema_from_ddl_text(ddl, dialect="postgres")

    assert result.skipped == {
        "non_create_table": 2,  # CREATE INDEX + GRANT
        "ctas": 1,
        "constraints_only": 1,
    }
    assert result.skipped_statement_count == 4


def test_trailing_comment_is_not_counted_as_a_skipped_statement() -> None:
    """脚本尾部的说明行根本不是语句,计进 skipped 会让用户以为丢了东西。"""
    result = schema_from_ddl_text(
        "CREATE TABLE ods.t (a INT);\n-- 以上为订单相关表\n", dialect="postgres"
    )

    assert result.skipped == {}
    assert result.table_count == 1


def test_summary_reports_applied_counts_not_parsed_counts() -> None:
    """★ 生效数是**合并**才知道的事实:被缓存完全遮蔽的表贡献 0,不能报 1。"""
    context = {
        "default_schema": None,
        "schema": {"ods": {"orders": {"id": "integer", "amount": "numeric"}}},
    }

    summary = apply_ddl_schema(
        context,
        "CREATE TABLE ods.orders (id INT, amount DECIMAL(12,2));",
        dialect="postgres",
        default_schema=None,
    )

    assert summary is not None
    # 解析出 1 张表 2 列,但全被缓存遮蔽 → 实际贡献 0。
    assert summary["table_count"] == 0
    assert summary["column_count"] == 0
    assert summary["parsed_table_count"] == 1
    assert summary["parsed_column_count"] == 2
    # 缓存那份原封不动。
    assert context["schema"] == {"ods": {"orders": {"id": "integer", "amount": "numeric"}}}


def test_summary_counts_only_the_tables_ddl_actually_contributed() -> None:
    context = {"default_schema": None, "schema": {"ods": {"orders": {"id": "integer"}}}}

    summary = apply_ddl_schema(
        context,
        "CREATE TABLE ods.orders (id INT);\nCREATE TABLE ods.dwd (a INT, b INT);",
        dialect="postgres",
        default_schema=None,
    )

    assert summary is not None
    assert (summary["table_count"], summary["column_count"]) == (1, 2)
    assert (summary["parsed_table_count"], summary["parsed_column_count"]) == (2, 3)


def test_apply_ddl_schema_without_ddl_text_touches_nothing() -> None:
    context = {"default_schema": None, "schema": {"ods": {"orders": {"id": "integer"}}}}
    before = {"ods": {"orders": {"id": "integer"}}}

    assert apply_ddl_schema(context, None, dialect="postgres", default_schema=None) is None
    assert apply_ddl_schema(context, "   ", dialect="postgres", default_schema=None) is None
    assert context["schema"] == before


def test_summary_reports_the_dialect_actually_used() -> None:
    summary = apply_ddl_schema(
        {"default_schema": None, "schema": {}},
        "CREATE TABLE ods.t (a INT);",
        dialect="dameng",
        default_schema=None,
    )

    assert summary is not None
    assert summary["dialect"] == "dm"


# ── F8 缓存键必须区分"带 DDL"与"不带 DDL" ────────────────────────────


def _hash_with(ddl_text: str | None, schema_context: dict[str, Any]) -> str:
    return lineage_sql_hash(
        sql_text="INSERT INTO ods.dwd SELECT * FROM ods.orders",
        dialect="postgres",
        schema_context=schema_context,
        ddl_source=lineage_ddl_fingerprint(ddl_text, dialect="postgres", default_schema=None),
    )


def test_empty_merge_still_yields_a_different_hash_than_no_ddl() -> None:
    """★ 合并是空操作时,缓存键**必须**还能区分带没带 DDL。

    取合并后的 schema_context 算 hash 就区分不开,两个方向都串位:
    带 DDL 的运行被不带 DDL 的请求命中(界面显示从未提交过的 DDL 徽标),
    以及带有效 DDL 的请求命中旧的无 DDL 运行(徽标不出现,skipped 计数丢失)。
    """
    # DDL 表已全在缓存中 → 合并后 schema_context 与不带 DDL 时逐字节相同。
    context = {"default_schema": None, "schema": {"ods": {"orders": {"id": "integer"}}}}
    ddl = "CREATE TABLE ods.orders (id INT);"
    merged = {"default_schema": None, "schema": dict(context["schema"])}
    apply_ddl_schema(merged, ddl, dialect="postgres", default_schema=None)

    assert merged["schema"] == context["schema"], "前提:这一份合并确实是空操作"
    assert _hash_with(ddl, merged) != _hash_with(None, context)


def test_all_statements_skipped_still_yields_a_different_hash() -> None:
    """另一种空操作:DDL 全被跳过(比如整段都不是建表语句)。"""
    context = {"default_schema": None, "schema": {"ods": {"orders": {"id": "integer"}}}}

    assert _hash_with("GRANT SELECT ON ods.orders TO reader;", context) != _hash_with(None, context)


def test_different_ddl_text_yields_different_hash() -> None:
    context = {"default_schema": None, "schema": {}}

    assert _hash_with("CREATE TABLE ods.a (x INT);", context) != _hash_with(
        "CREATE TABLE ods.a (y INT);", context
    )


def test_hash_without_ddl_is_unchanged_by_the_new_field() -> None:
    """不给 DDL 的请求 hash 与从前逐字节一致 —— 既有缓存不因本次改动整体失效。"""
    context = {"default_schema": None, "schema": {"ods": {"orders": {"id": "integer"}}}}
    legacy = lineage_sql_hash(
        sql_text="INSERT INTO ods.dwd SELECT * FROM ods.orders",
        dialect="postgres",
        schema_context=context,
    )

    assert _hash_with(None, context) == legacy
    assert _hash_with("   ", context) == legacy


# ── 端到端:DDL 把表级降级重新拉回列级 ────────────────────────────────

_INSERT_SQL = (
    "INSERT INTO ODS.DWD_ORDERS (ORDER_ID, AMT) SELECT O.ORDER_ID, O.AMT * 1.1 FROM ODS.ORDERS O"
)

_DM_DDL = """
CREATE TABLE "ODS"."ORDERS" ("ORDER_ID" NUMBER(18), "AMT" NUMBER(12,2),
  NOT CLUSTER PRIMARY KEY("ORDER_ID")) STORAGE(ON "MAIN", CLUSTERBTR);
CREATE TABLE "ODS"."DWD_ORDERS" ("ORDER_ID" NUMBER(18), "AMT" NUMBER(12,2));
"""


def test_without_ddl_missing_metadata_still_degrades_to_table_level() -> None:
    """基线:无元数据 + 无 DDL —— 维持现状的表级降级(本 PR 不改这条路)。"""
    report = analyze_sql_lineage(
        LineageParseRequest(sql_text=_INSERT_SQL, dialect="dm", schema={}, lenient=True)
    )

    assert len(report.graph_edges) == 1
    assert report.insert_mappings == []
    assert any(warning["code"] == "lenient_table_level" for warning in report.warnings)


def test_ddl_schema_restores_column_level_lineage_without_a_database() -> None:
    """无库 + 有 DDL → 列级血缘(本 PR 的验收点)。"""
    ddl = schema_from_ddl_text(_DM_DDL, dialect="dm")

    report = analyze_sql_lineage(
        LineageParseRequest(sql_text=_INSERT_SQL, dialect="dm", schema=ddl.schema, lenient=True)
    )

    mappings = {
        (mapping["source_column"], mapping["target_column"]) for mapping in report.insert_mappings
    }
    assert ("ORDER_ID", "ORDER_ID") in mappings
    assert ("AMT", "AMT") in mappings
    assert not any(warning["code"] == "lenient_table_level" for warning in report.warnings)


# ── 各家导出工具的原样输出形态 ────────────────────────────────────────────


def test_pg_dump_shape_does_not_inject_tables_from_function_bodies() -> None:
    """F2a:``$$`` 函数体不认就会被按体内分号切开,体内建的临时表被当真表注入。

    注入的表在无缓存时会成为该表的**权威列定义**,血缘对着虚构列产出结果,还写进缓存。
    """
    ddl = """
    CREATE TABLE public.orders (id integer, amount numeric);
    CREATE FUNCTION public.f() RETURNS void AS $$
    BEGIN
      RAISE NOTICE 'building';
      CREATE TABLE orders (wrong_col text);
    END;
    $$ LANGUAGE plpgsql;
    CREATE TABLE public.customers (id integer, name text);
    """
    result = schema_from_ddl_text(ddl, dialect="postgres")

    assert result.schema == {
        "public": {
            "orders": {"id": "INT", "amount": "DECIMAL"},
            "customers": {"id": "INT", "name": "TEXT"},
        }
    }
    # 函数体里的 orders(wrong_col)绝不能进来。
    assert "wrong_col" not in str(result.schema)


def test_pg_dump_dollar_quote_with_tag() -> None:
    ddl = """
    CREATE FUNCTION f() RETURNS void AS $body$
    BEGIN
      CREATE TABLE ghost (nope text);
    END;
    $body$ LANGUAGE plpgsql;
    CREATE TABLE public.real_t (id integer);
    """
    result = schema_from_ddl_text(ddl, dialect="postgres")

    assert result.schema == {"public": {"real_t": {"id": "INT"}}}


def test_mysqldump_shape_survives_backslash_escaped_quotes() -> None:
    """F2b:``VALUES (1,'it\\'s here')`` 让字符串扫描提前结束,吞掉后续全部建表。"""
    ddl = (
        "CREATE TABLE `orders` (`id` int NOT NULL, `note` varchar(64)) ENGINE=InnoDB;\n"
        "INSERT INTO `orders` VALUES (1,'it\\'s here');\n"
        "CREATE TABLE `customers` (`id` int NOT NULL, `name` varchar(64)) ENGINE=InnoDB;\n"
    )
    result = schema_from_ddl_text(ddl, dialect="mysql")

    assert set(result.schema[""]) == {"orders", "customers"}


def test_mysql_hash_line_comment_is_recognised() -> None:
    ddl = "CREATE TABLE `t` (`a` int, # 说明\n`b` int);"
    result = schema_from_ddl_text(ddl, dialect="mysql")

    assert result.schema == {"": {"t": {"a": "INT", "b": "INT"}}}


def test_postgres_nested_block_comment_does_not_break_the_scanner() -> None:
    ddl = "CREATE TABLE ods.t (/* outer /* inner */ still comment */ a int, b int);"
    result = schema_from_ddl_text(ddl, dialect="postgres")

    assert result.schema == {"ods": {"t": {"a": "INT", "b": "INT"}}}


def test_ssms_go_batch_separator_splits_statements() -> None:
    """F9:SSMS「Generate Scripts」用 GO 分批,不认就丢掉首个 GO 之后的全部建表。"""
    ddl = (
        "CREATE TABLE dbo.orders (id int, amount decimal(18,2))\n"
        "GO\n"
        "CREATE TABLE dbo.customers (id int, name nvarchar(64))\n"
        "GO\n"
    )
    result = schema_from_ddl_text(ddl, dialect="tsql")

    assert set(result.schema["dbo"]) == {"orders", "customers"}
    assert result.skipped_statement_count == 0


def test_go_inside_an_identifier_is_not_a_batch_separator() -> None:
    """``GO`` 必须独占一行才算分隔符,别把 ``go_live_dt`` 之类切开。"""
    result = schema_from_ddl_text("CREATE TABLE dbo.t (go_live_dt date, ago int)", dialect="tsql")

    assert result.schema == {"dbo": {"t": {"go_live_dt": "DATE", "ago": "INTEGER"}}}


def test_template_variables_in_ddl_are_normalized() -> None:
    """F10:``${VAR}`` 要和 sql_text 路径一样先归一,否则解析失败只计一次无理由 skipped。"""
    result = schema_from_ddl_text(
        "CREATE TABLE ${ODS_SCHEMA}.T (id INT, amt DECIMAL(18,2));", dialect="oracle"
    )

    assert result.skipped_statement_count == 0
    assert result.table_count == 1
    # 占位符 schema 无法当成真 schema 名,归入 "" 桶(parser 侧是"扫所有桶"语义)。
    assert result.schema[""]["T"] == {"id": "INT", "amt": "NUMBER(18, 2)"}


def test_same_table_written_in_two_cases_is_one_entry() -> None:
    """大小写只是写法差异,不是两张表 —— 否则计数翻倍、取哪份列集看插入顺序。"""
    ddl = 'CREATE TABLE "ODS"."T" ("A" INT);\nCREATE TABLE ods.t (a INT, b INT);'
    result = schema_from_ddl_text(ddl, dialect="dm")

    assert result.table_count == 1
    assert len(result.schema) == 1
    assert result.column_count == 2


# ── 达梦导出实拍形态:五个坑同时出现在一份 DDL 里 ─────────────────────────
#
# 这一份是 F3–F6 的合并验收点。同时具备:
#   1. 表级 NOT CLUSTER PRIMARY KEY("ID")   —— 漏过即挂死(F4)
#   2. 约束行带前置中文注释                  —— 绕过预过滤(F4)
#   3. 每列尾部中文 -- 注释                  —— 吞掉右括号,整表丢弃(F5)
#   4. NOT NULL ENABLE 后缀                  —— 单条目失败拖垮整表(F6)
#   5. period 裸列名                         —— 被首词匹配整列吞掉(F3)
# 这一份能出列级血缘,才算 F3–F6 真的修好。

_DM_REAL_SHAPE_DDL = '''CREATE TABLE "ODS"."KGRP_RPT"
(
  "ID" NUMBER(18,0) NOT NULL ENABLE, -- 主键标识
  period VARCHAR2(8), -- 账期分区(裸列名,非保留字)
  "AMT" NUMBER(18,2) DEFAULT 0, -- 金额
  "NOTE" VARCHAR2(200), -- 备注说明
  -- 主键约束
  NOT CLUSTER PRIMARY KEY("ID")
) STORAGE(ON "MAIN", CLUSTERBTR);
'''


def test_dm_real_export_shape_yields_all_columns() -> None:
    result = schema_from_ddl_text(_DM_REAL_SHAPE_DDL, dialect="dm")

    assert result.schema == {
        "ODS": {
            "KGRP_RPT": {
                "period": "VARCHAR2(8)",
                "AMT": "NUMBER(18, 2)",
                "NOTE": "VARCHAR2(200)",
            }
        }
    }
    assert result.skipped_statement_count == 0
    # "ID" 那条因 ENABLE 解析不动 —— 只损失该列,并且诚实报出来。
    assert result.failed_column_entry_count == 1


def test_dm_real_export_shape_gives_column_level_lineage() -> None:
    """★ 终极验收:这份 DDL 要能真的产出列级血缘,不只是解析出列名。"""
    ddl = schema_from_ddl_text(_DM_REAL_SHAPE_DDL, dialect="dm")
    sql = (
        "INSERT INTO ODS.KGRP_SUM (period, AMT) "
        "SELECT R.period, SUM(R.AMT) FROM ODS.KGRP_RPT R GROUP BY R.period"
    )
    schema = merge_ddl_schema(
        {"ODS": {"KGRP_SUM": {"period": "VARCHAR2(8)", "AMT": "NUMBER(18,2)"}}}, ddl.schema
    )

    report = analyze_sql_lineage(
        LineageParseRequest(sql_text=sql, dialect="dm", schema=schema, lenient=True)
    )

    # dm / Oracle 把裸标识符归一成大写,比对时统一大小写(是正确行为,不是缺陷)。
    mappings = {
        (mapping["source_column"].upper(), mapping["target_column"].upper())
        for mapping in report.insert_mappings
    }
    assert ("PERIOD", "PERIOD") in mappings
    assert ("AMT", "AMT") in mappings
    assert not any(warning["code"] == "lenient_table_level" for warning in report.warnings)


# ── F1 长期护栏:带 DDL 的结果永远不劣于不带 DDL ──────────────────────────
#
# 价值判断 #1(任务书总纲):DDL 是补充信息,任何情况下都不该让血缘变差。
# 这条护栏刻意用"同一段 SQL 跑两遍比计数"的形态,而不是断言某个具体数字 ——
# 将来 DDL 预处理再怎么改,这条断言都还成立才算没砸锅。

_F1_BASE_SCHEMA = {
    "ODS": {
        "SRC": {"ID": "integer", "AMT": "numeric"},
        "TGT": {"ID": "integer", "AMT": "numeric"},
    }
}


@pytest.mark.parametrize(
    ("label", "sql", "default_schema", "ddl"),
    [
        (
            "裸表名 DDL + 未限定 SQL + 无 default_schema(F1 实测退化形态)",
            "INSERT INTO TGT (ID, AMT) SELECT ID, AMT FROM SRC",
            None,
            "CREATE TABLE SRC (ID INT, AMT DECIMAL(18,2));\n"
            "CREATE TABLE TGT (ID INT, AMT DECIMAL(18,2));",
        ),
        (
            "裸表名 DDL + 限定 SQL",
            "INSERT INTO ODS.TGT (ID, AMT) SELECT ID, AMT FROM ODS.SRC",
            "ODS",
            "CREATE TABLE SRC (ID INT, AMT DECIMAL(18,2));",
        ),
        (
            "DDL 列比缓存少(不得让缓存降级)",
            "INSERT INTO TGT (ID, AMT) SELECT ID, AMT FROM SRC",
            "ODS",
            "CREATE TABLE SRC (ID INT);",
        ),
        (
            "DDL 全是垃圾(不得比不给 DDL 更差)",
            "INSERT INTO ODS.TGT (ID, AMT) SELECT ID, AMT FROM ODS.SRC",
            "ODS",
            "not sql at all ;;; ((( ;",
        ),
    ],
)
def test_ddl_never_degrades_lineage_versus_no_ddl(
    label: str, sql: str, default_schema: str | None, ddl: str
) -> None:
    """F1 护栏:同一段 SQL,带 DDL 的列映射 / 列数不得少于不带 DDL。"""

    def run(schema: dict[str, dict[str, dict[str, str]]]) -> tuple[int, int, bool]:
        report = analyze_sql_lineage(
            LineageParseRequest(
                sql_text=sql,
                dialect="dm",
                schema=schema,
                default_schema=default_schema,
                lenient=True,
            )
        )
        degraded = any(w["code"] == "lenient_table_level" for w in report.warnings)
        return len(report.insert_mappings), len(report.columns), degraded

    baseline = run(_F1_BASE_SCHEMA)
    merged = merge_ddl_schema(
        _F1_BASE_SCHEMA, schema_from_ddl_text(ddl, dialect="dm", default_schema=None).schema
    )
    with_ddl = run(merged)

    assert with_ddl[0] >= baseline[0], f"{label}: 列映射变少了 {baseline[0]} → {with_ddl[0]}"
    assert with_ddl[1] >= baseline[1], f"{label}: 列数变少了 {baseline[1]} → {with_ddl[1]}"
    assert not (with_ddl[2] and not baseline[2]), f"{label}: 带 DDL 反而降级成表级"
