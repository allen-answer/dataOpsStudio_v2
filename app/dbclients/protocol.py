from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol

from app.domain.capabilities import AdapterCapabilities
from app.domain.plan import PlanNode
from app.domain.schema import Column, Index, Row, Schema, Table


class DatabaseAdapter(Protocol):
    """用户数据库 adapter 接口(契约 §3.2、设计稿 §2.9)。

    2.0.0 推进顺序:MySQL(1,骨架验证)→ DM(2)→ Oracle(3,可滑 2.0.x)→ DB2(4,Preview)。

    ★ 骨架设计约束:首个写 MySQL,但接口设计须以 **Oracle/DM 为假想验证对象**:
    - 标识符大小写(Oracle 默认大写;MySQL 反引号原样)
    - 分页语法(LIMIT vs ROWNUM/FETCH FIRST)
    - cursor 语义(MySQL SSCursor server-side vs 其它驱动 fetchmany)
    - 类型映射

    ★ server_side_cancel 多为 False(1.x AS-IS §2 已记录:MySQL/Oracle/DM/DB2
    多无 driver-level cancel,kill_query 实际是软取消)—— 调用方按 capabilities 降级。

    R1 红线:DB 驱动 import 只允许在 app/dbclients/ + app/infrastructure/(本目录已豁免 TID251)。
    """

    capabilities: AdapterCapabilities

    def execute_select(self, sql: str, params: dict[str, Any]) -> Iterator[Row]:
        """流式 SELECT 执行。worker 用 fetchmany 拉,写 spool。"""
        raise NotImplementedError

    def explain(self, sql: str) -> PlanNode:
        raise NotImplementedError

    def stream_rows(self, sql: str) -> Iterator[Row]:
        raise NotImplementedError

    def test_connection(self) -> bool:
        raise NotImplementedError

    def list_schemas(self) -> list[Schema]:
        raise NotImplementedError

    def list_tables(self, schema: str) -> list[Table]:
        raise NotImplementedError

    def list_columns(self, schema: str, table: str) -> list[Column]:
        raise NotImplementedError

    def list_indexes(self, schema: str, table: str) -> list[Index]:
        raise NotImplementedError

    def get_table_ddl(self, schema: str, table: str) -> str:
        raise NotImplementedError

    def kill_query(self, connection_id: str) -> bool:
        """driver-level cancel(多数 adapter 返回 False,走软取消)。"""
        raise NotImplementedError
