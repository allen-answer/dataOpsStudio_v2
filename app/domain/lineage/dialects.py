from __future__ import annotations

from sqlglot import Dialect
from sqlglot.dialects.oracle import Oracle


class DM(Oracle):
    """DM SQL is Oracle-compatible for the DML parsed in W1-PR1."""


def register_lineage_dialects() -> None:
    Dialect.classes.setdefault("dm", DM)


register_lineage_dialects()


__all__ = ["DM", "register_lineage_dialects"]
