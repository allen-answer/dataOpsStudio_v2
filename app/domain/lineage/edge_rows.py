"""确定性血缘边行构造纯函数(去重 API analyze 与 worker persist_run 两处落库)。

零 DB 依赖、零副作用(除 ``id_factory`` 生成主键 id):输入确定性解析结果里的
``graph_edges`` / ``insert_mappings``,输出可直接批量 insert 的行 dict 列表。
两处落库调用点(``api/routes/core.py:_insert_lineage_edges`` 与
``worker.py:PostgresLineageCatalog.persist_run``)共用本函数,保证表结构、
类型强制与过滤谓词口径完全一致。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4


def _default_edge_id() -> str:
    return str(uuid4())


def build_lineage_edge_rows(
    *,
    run_id: str,
    project_id: str,
    sql_hash: str,
    graph_edges: list[dict[str, Any]],
    insert_mappings: list[dict[str, Any]],
    id_factory: Callable[[], str] = _default_edge_id,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """构造 (table_rows, column_rows):确定性血缘边(inferred=False,confidence=1.0)。

    - table_rows:``graph_edges`` 里源/目标表齐全的边(edge_kind=table)
    - column_rows:``insert_mappings`` 里源/目标表列四元齐全的映射

    行结构与过滤谓词与 1.x 落库口径一致;主键 id 由 ``id_factory`` 生成
    (默认 uuid4;测试可注入确定性工厂以做逐字节断言)。
    """
    table_rows = [
        {
            "id": id_factory(),
            "run_id": run_id,
            "project_id": project_id,
            "source_table": str(edge["source_table"]),
            "target_table": str(edge["target_table"]),
            "edge_kind": "table",
            "inferred": False,
            "inference_status": "confirmed",
            "confidence": 1.0,
            "sql_hash": sql_hash,
        }
        for edge in graph_edges
        if edge.get("source_table") and edge.get("target_table")
    ]
    column_rows = [
        {
            "id": id_factory(),
            "run_id": run_id,
            "project_id": project_id,
            "source_table": str(mapping["source_table"]),
            "source_column": str(mapping["source_column"]),
            "target_table": str(mapping["target_table"]),
            "target_column": str(mapping["target_column"]),
            "transformation": str(mapping.get("transformation") or "DIRECT"),
            "transformation_subtype": str(mapping.get("transformation_subtype") or "DIRECT"),
            "inferred": False,
            "inference_status": "confirmed",
            "confidence": 1.0,
            "sql_hash": sql_hash,
        }
        for mapping in insert_mappings
        if (
            mapping.get("source_table")
            and mapping.get("source_column")
            and mapping.get("target_table")
            and mapping.get("target_column")
        )
    ]
    return table_rows, column_rows
