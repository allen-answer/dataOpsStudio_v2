"""DDL 文本数据源(app/domain/lineage/ddl_schema.py)单测。

覆盖:方言 / 注释 / 引号标识符 / 裸表名 + 默认 schema / 垃圾输入不炸 /
达梦导出实拍形态 / 与元数据缓存合并的优先级 / 端到端拿回列级血缘。
"""

from __future__ import annotations

import pytest

from app.domain.lineage import (
    LineageParseRequest,
    analyze_sql_lineage,
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
    assert result.as_summary() == {
        "table_count": 1,
        "column_count": 2,
        "skipped_statement_count": 0,
    }


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
        "FOREIGN KEY (cust_id) REFERENCES ods.cust (id)",
        "CHECK (amt >= 0)",
        "KEY idx_b (b)",
        "INDEX idx_c (c)",
    ],
)
def test_constraint_entries_are_filtered_before_sqlglot(entry: str) -> None:
    """★ 回归护栏:约束条目一律不进 sqlglot。

    ``NOT CLUSTER PRIMARY KEY`` 若漏过这层过滤会让解析器挂死(不是变慢,是不返回)。
    """
    assert _is_constraint_entry(entry) is True


@pytest.mark.parametrize(
    "entry",
    [
        "id INT",
        '"NOT_NULL_FLAG" CHAR(1)',
        "amount DECIMAL(12,2) DEFAULT 0",
        "`key_name` VARCHAR(20)",
    ],
)
def test_column_entries_are_not_filtered(entry: str) -> None:
    assert _is_constraint_entry(entry) is False


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
