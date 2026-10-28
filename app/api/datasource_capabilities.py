"""数据源能力下发:前端不再各自硬编码 db_type 白名单,改读后端算好的能力(设计稿 §3.3)。

由 **适配器类 capabilities、会话缝可用性、数据源 operation_policy、成熟度四者** 计算,不连库。

“Preview 档放开 SQL 执行需单独 PR 人拍板”的治理点收在 `PREVIEW_EXECUTION_ALLOWED`
这一个常量上(原先散在前端 SqlWorkspaceView 的白名单里)。

| 能力 | 规则 |
|---|---|
| execute | Certified(mysql / dm),或在 PREVIEW_EXECUTION_ALLOWED |
| sql_tools / metadata | execute 且适配器支持 explain(DB2 不支持) |
| explain | sql_tools 且 allow_explain;Oracle 另需 allow_oracle_plan_table |
| session_console | broker 的 SESSION_CAPABLE_DB_TYPES |
"""

from __future__ import annotations

from typing import Literal

from app.api.schemas import DatasourceCapabilities
from app.broker.wiring import SESSION_CAPABLE_DB_TYPES
from app.dbclients.factory import adapter_class_for
from app.domain.datasource import DbType, OperationPolicy

Maturity = Literal["certified", "preview"]

CERTIFIED_DB_TYPES: frozenset[DbType] = frozenset({DbType.MYSQL, DbType.DM})

# Preview 档里已放开 SQL 执行的类型。改它须人拍板。
# Oracle / OB-Oracle:2026-10-27 经用户同意以 Preview 档放开(设计稿 §12 PR-6);
# 工作台只放行只读 SQL,适配器已覆盖 11g ROWNUM、NUMBER 精度、OB 扩展类型与转义。
PREVIEW_EXECUTION_ALLOWED: frozenset[DbType] = frozenset(
    {DbType.POSTGRESQL, DbType.DB2, DbType.ORACLE, DbType.OCEANBASE_ORACLE}
)

# EXPLAIN 需写会话临时表 PLAN_TABLE 的类型:按钮另需数据源 allow_oracle_plan_table。
_PLAN_TABLE_EXPLAIN_TYPES: frozenset[DbType] = frozenset({DbType.ORACLE})


def datasource_capabilities(
    db_type: DbType, operation_policy: OperationPolicy
) -> DatasourceCapabilities:
    adapter_class = adapter_class_for(db_type)
    caps = getattr(adapter_class, "capabilities", None)
    maturity: Maturity = "certified" if db_type in CERTIFIED_DB_TYPES else "preview"
    # execute / sql_tools 表达“该类型在本 build 是否开放”,不叠加 allow_select:
    # SELECT 权限仍由执行入口的 operation_policy 校验报错(与收敛前一致,前端不会把
    # “策略关了查询”误显示成“类型不支持”)。explain 按钮照旧叠加 allow_explain。
    execute = (
        caps is not None
        and caps.execute_select
        and (maturity == "certified" or db_type in PREVIEW_EXECUTION_ALLOWED)
    )
    sql_tools = execute and caps is not None and caps.explain
    explain = sql_tools and operation_policy.allow_explain
    if db_type in _PLAN_TABLE_EXPLAIN_TYPES:
        # 闸门关时适配器连库前即拒;不下发一个必然报错的按钮
        explain = explain and operation_policy.allow_oracle_plan_table
    return DatasourceCapabilities(
        execute=execute,
        explain=explain,
        ddl=bool(caps is not None and caps.get_table_ddl),
        metadata=sql_tools,
        sql_tools=sql_tools,
        session_console=db_type in SESSION_CAPABLE_DB_TYPES,
        compare_db_hash=bool(caps is not None and caps.compare_db_hash),
        maturity=maturity,
    )


__all__ = ["CERTIFIED_DB_TYPES", "PREVIEW_EXECUTION_ALLOWED", "datasource_capabilities"]
