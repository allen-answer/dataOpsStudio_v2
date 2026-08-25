from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

# v3: normalize ${...} template variables before sqlglot and persist structured
# ParseError location details. Bumping invalidates cached failures from v2.
LINEAGE_PARSER_VERSION = "sqlglot-w1-v3"


def lineage_ddl_fingerprint(
    ddl_text: str | None,
    *,
    dialect: str,
    default_schema: str | None,
) -> dict[str, str] | None:
    """DDL 文本数据源的缓存指纹;没给 DDL 时为 ``None``。

    取 **原文** 的摘要,而不是合并后的 schema_context —— 合并可能是空操作(语句全被
    跳过,或 DDL 表已全在缓存中),那时带 DDL 与不带 DDL 会算出相同的 hash,两个方向
    都串位:先用错方言的 DDL 分析一次并持久化摘要,之后同一 SQL **不带** DDL 的请求
    命中该缓存,界面显示从未提交过的 DDL 徽标;反向则是带有效 DDL 的请求命中旧的
    无 DDL 运行,徽标不出现,skipped 计数这一唯一反馈丢失。
    """
    if not ddl_text or not ddl_text.strip():
        return None
    return {
        "dialect": dialect.lower(),
        "default_schema": default_schema or "",
        "ddl_sha256": sha256(ddl_text.encode("utf-8")).hexdigest(),
    }


def lineage_sql_hash(
    *,
    sql_text: str,
    dialect: str,
    schema_context: dict[str, Any],
    parser_version: str = LINEAGE_PARSER_VERSION,
    ddl_source: dict[str, str] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "dialect": dialect.lower(),
        "parser_version": parser_version,
        "schema_context": schema_context,
        "sql_text": sql_text,
    }
    # 只在真给了 DDL 时加这个键:不给 DDL 的请求 hash 与从前逐字节一致,
    # 既有缓存不会因为本次改动整体失效。
    if ddl_source:
        payload["ddl_source"] = ddl_source
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()
