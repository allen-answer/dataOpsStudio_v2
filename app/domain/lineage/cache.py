from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

# v2: statement coverage widened to CTAS / MERGE / multi-table & correlated
# UPDATE / CTE / UNION — bumping invalidates cached parses from the v1 parser.
LINEAGE_PARSER_VERSION = "sqlglot-w1-v2"


def lineage_sql_hash(
    *,
    sql_text: str,
    dialect: str,
    schema_context: dict[str, Any],
    parser_version: str = LINEAGE_PARSER_VERSION,
) -> str:
    payload = {
        "dialect": dialect.lower(),
        "parser_version": parser_version,
        "schema_context": schema_context,
        "sql_text": sql_text,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()
