from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TransformationKind(StrEnum):
    DIRECT = "DIRECT"
    INDIRECT = "INDIRECT"


class TransformationSubtype(StrEnum):
    DIRECT = "DIRECT"
    AGGREGATION = "AGGREGATION"
    TRANSFORMATION = "TRANSFORMATION"
    FILTER = "FILTER"
    JOIN = "JOIN"
    CAST = "CAST"
    EXPRESSION = "EXPRESSION"


class TableLineageEdge(BaseModel):
    source_table: str
    target_table: str
    statement_index: int = 0
    inferred: bool = False


class InsertMapping(BaseModel):
    target_table: str
    target_column: str
    source_table: str
    source_column: str
    statement_index: int = 0
    transformation: TransformationKind = TransformationKind.DIRECT
    transformation_subtype: TransformationSubtype = TransformationSubtype.DIRECT
    inferred: bool = False


class LineageParseError(BaseModel):
    statement_index: int
    error_type: str
    message: str
    unsupported: bool = False
    statement_type: str | None = None


class LineageWarning(BaseModel):
    code: str
    message: str
    statement_index: int | None = None


class TargetSummary(BaseModel):
    table: str
    operation: str
    refresh_mode: str | None = None
    statement_index: int = 0


class LineageReport(BaseModel):
    """1.x-compatible LineageReport envelope.

    The 1.x model also has ``model_config``; the business envelope fields below
    were read from the deployed 1.x app before implementing this module.
    """

    model_config = ConfigDict(extra="allow")

    statement_count: int = Field(default=0, ge=0)
    tables: list[dict[str, Any]] = Field(default_factory=list)
    columns: list[dict[str, Any]] = Field(default_factory=list)
    insert_mappings: list[dict[str, Any]] = Field(default_factory=list)
    target_summary: list[TargetSummary] = Field(default_factory=list)
    table_roles: list[dict[str, Any]] = Field(default_factory=list)
    joins: list[dict[str, Any]] = Field(default_factory=list)
    filters: list[dict[str, Any]] = Field(default_factory=list)
    group_by: list[dict[str, Any]] = Field(default_factory=list)
    unions: list[dict[str, Any]] = Field(default_factory=list)
    variables: list[dict[str, Any]] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    dynamic_sql_count: int = Field(default=0, ge=0)
    dynamic_sql_segments: list[dict[str, Any]] = Field(default_factory=list)
    procedure_segments: list[dict[str, Any]] = Field(default_factory=list)
    graph_edges: list[dict[str, Any]] = Field(default_factory=list)
    graph_groups: list[dict[str, Any]] = Field(default_factory=list)
    parse_errors: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    statements: list[dict[str, Any]] = Field(default_factory=list)
    semantic_lineage: dict[str, Any] = Field(default_factory=dict)
    report: dict[str, Any] | None = None
