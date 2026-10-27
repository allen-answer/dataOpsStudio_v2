"""数据源能力下发:前端不再各自硬编码 db_type 白名单,改读后端算好的能力(设计稿 §3.3)。

由 **适配器类 capabilities、会话缝可用性、数据源 operation_policy、成熟度四者** 计算,不连库。

“Preview 档放开 SQL 执行需单独 PR 人拍板”的治理点收在 `PREVIEW_EXECUTION_ALLOWED`
这一个常量上(原先散在前端 SqlWorkspaceView 的白名单里)。本模块的取值复刻收敛前前端
白名单的现状,不改变任何类型的可用范围:

| 能力 | 收敛前前端白名单 | 本模块规则 |
|---|---|---|
| execute | mysql / dm / postgresql / db2 | Certified,或在 PREVIEW_EXECUTION_ALLOWED |
| sql_tools / metadata | mysql / dm / postgresql | execute 且适配器支持 explain(DB2 不支持) |
| explain | 同上且 allow_explain | sql_tools 且 operation_policy.allow_explain |
| session_console | mysql / dm | broker 的 SESSION_CAPABLE_DB_TYPES |
"""

from __future__ import annotations

from typing import Literal

from app.api.schemas import DatasourceCapabilities
from app.broker.wiring import SESSION_CAPABLE_DB_TYPES
from app.dbclients.factory import adapter_class_for
from app.domain.datasource import DbType, OperationPolicy

Maturity = Literal["certified", "preview"]

CERTIFIED_DB_TYPES: frozenset[DbType] = frozenset({DbType.MYSQL, DbType.DM})

# Preview 档里已放开 SQL 执行的类型。改它须人拍板(Oracle / OB-Oracle 待 PR-6 决定)。
PREVIEW_EXECUTION_ALLOWED: frozenset[DbType] = frozenset({DbType.POSTGRESQL, DbType.DB2})


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
    return DatasourceCapabilities(
        execute=execute,
        explain=sql_tools and operation_policy.allow_explain,
        ddl=bool(caps is not None and caps.get_table_ddl),
        metadata=sql_tools,
        sql_tools=sql_tools,
        session_console=db_type in SESSION_CAPABLE_DB_TYPES,
        compare_db_hash=bool(caps is not None and caps.compare_db_hash),
        maturity=maturity,
    )


__all__ = ["CERTIFIED_DB_TYPES", "PREVIEW_EXECUTION_ALLOWED", "datasource_capabilities"]
