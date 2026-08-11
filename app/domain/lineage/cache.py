from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

# v3: normalize ${...} template variables before sqlglot and persist structured
# ParseError location details. Bumping invalidates cached failures from v2.
LINEAGE_PARSER_VERSION = "sqlglot-w1-v3"


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
