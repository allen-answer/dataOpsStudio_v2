from __future__ import annotations

from pydantic import BaseModel


class AdapterCapabilities(BaseModel):
    """DatabaseAdapter 声明的能力矩阵(契约 §3.2、设计稿 §2.9)。

    调用方在调用对应方法前必须先检查 capability,不支持的能力优雅降级
    (前端隐藏按钮 / 后端 405 / 等),不假设所有 adapter 全功能。

    ★ server_side_cancel 多为 False —— 1.x AS-IS §2 已记录:
    MySQL/Oracle/DM/DB2 多无 driver-level cancel,kill_query 实际是"软取消"
    (cancel_requested flag + statement timeout 兜底),不要假设能真 kill。
    """

    execute_select: bool
    explain: bool
    stream_rows: bool
    server_side_cancel: bool
    list_schemas: bool
    list_tables: bool
    list_columns: bool
    list_indexes: bool
    get_table_ddl: bool
    compare_db_hash: bool = False
