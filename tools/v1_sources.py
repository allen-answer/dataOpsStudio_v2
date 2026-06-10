"""1.x 数据源读取(纯 stdlib)—— migrate_from_v1.py 的输入侧。

只负责"把 1.x 的 config/*.json + data/dataops.db 读成 Python 结构",
不做任何 2.0 写入、不碰 Fernet、不连 PG。读取边界清晰,便于单测构造 fixture。

数据形态依据 docs/legacy/V1_AS_IS.md §3(只信此文档,不翻 1.x 源码猜)。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class V1SourceError(RuntimeError):
    """1.x 源数据读取失败。"""


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise V1SourceError(f"invalid JSON in {path}: {exc}") from exc


def load_json_list(source_dir: Path, filename: str) -> list[dict[str, Any]]:
    """读 config/<filename>(list[dict] 形态)。文件缺失 → 空列表。"""
    path = source_dir / "config" / filename
    if not path.exists():
        return []
    data = _read_json(path)
    if not isinstance(data, list):
        raise V1SourceError(f"{path} is not a JSON array")
    return [row for row in data if isinstance(row, dict)]


def load_json_object(source_dir: Path, filename: str) -> dict[str, Any] | None:
    """读 config/<filename>(单 dict 形态,如 lineage_ai.json)。缺失 → None。"""
    path = source_dir / "config" / filename
    if not path.exists():
        return None
    data = _read_json(path)
    if not isinstance(data, dict):
        raise V1SourceError(f"{path} is not a JSON object")
    return data


def sqlite_path(source_dir: Path) -> Path:
    return source_dir / "data" / "dataops.db"


@contextmanager
def open_sqlite(source_dir: Path) -> Iterator[sqlite3.Connection | None]:
    """打开 1.x SQLite(data/dataops.db)。缺失 → yield None(调用方按"跳过"处理)。"""
    path = sqlite_path(source_dir)
    if not path.exists():
        yield None
        return
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def read_sqlite_rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    """读整张表为 list[dict]。表不存在 → 空列表(调用方记跳过原因)。"""
    if not sqlite_table_exists(conn, table):
        return []
    cur = conn.execute(f"SELECT * FROM {table}")
    return [dict(row) for row in cur.fetchall()]
