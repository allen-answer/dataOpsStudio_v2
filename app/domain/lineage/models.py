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
    # L-3 PR2 补链边元数据(游标/UDF/BULK COLLECT/TRIGGER)。确定性边(sqlglot 直解)
    # 三者恒为 None;启发式补链边打 edge_type + confidence="medium" + reason,让下游
    # 区分「确定性 vs 推断补链」。默认 None,envelope 向后兼容(1.x 契约不破)。
    edge_type: str | None = None
    confidence: str | None = None
    reason: str | None = None


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
    # 人话消息之外的原始诊断(如 sqlglot 的 token 级错误);排查时才展开
    detail: str | None = None
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
    # DELETE/TRUNCATE 才有意义:无 WHERE 的 DELETE = 全表清空(全量重刷判据)。
    # INSERT/CREATE/MERGE/UPDATE 恒为 None。
    has_where: bool | None = None


class RefreshMode(StrEnum):
    """目标表刷新方式。判定见 domain/lineage/semantic.py:classify_refresh_mode。"""

    TRUNCATE_INSERT = "truncate_insert"  # TRUNCATE+INSERT 全量重刷
    DELETE_INSERT = "delete_insert"  # 无 WHERE 的 DELETE+INSERT 全量重刷
    DELETE_INSERT_PARTIAL = "delete_insert_partial"  # 带 WHERE 的 DELETE+INSERT 分区/增量重刷
    MERGE = "merge"  # 仅 MERGE
    UPDATE = "update"  # 仅 UPDATE
    APPEND = "append"  # 仅 INSERT 追加
    MIXED = "mixed"  # 多种写混合但无删+插序列


class TableRoleKind(StrEnum):
    """表在数据流中的角色。结构角色(读写关系)+ 命名角色(schema/表名关键词)。"""

    TARGET = "target"  # 只写
    INTERMEDIATE = "intermediate"  # 既写又读(中转/staging)
    SOURCE_FACT = "source_fact"  # 纯读源(兜底)
    REMOTE_DBLINK = "remote_dblink"  # Oracle @dblink 远端
    CONFIG = "config"  # 配置表
    REFERENCE = "reference"  # 参照/字典/码表
    DIMENSION = "dimension"  # 维表
    FILTER = "filter"  # 过滤/黑名单表


class TableRole(BaseModel):
    table: str
    roles: list[str] = Field(default_factory=list)
    primary_role: str = TableRoleKind.SOURCE_FACT.value


class TargetIntegration(BaseModel):
    """单个目标表的整合视图(跨脚本聚合)。"""

    table: str
    primary_role: str = TableRoleKind.TARGET.value
    roles: list[str] = Field(default_factory=list)
    refresh_mode: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)  # insert/update/merge/delete/truncate
    source_tables: list[str] = Field(default_factory=list)
    source_files: list[str] = Field(default_factory=list)  # 批量:哪些脚本写本表
    titles: list[str] = Field(default_factory=list)


class SemanticRisk(BaseModel):
    level: str  # high / medium / low
    type: str
    message: str


class SemanticView(BaseModel):
    """L-4 语义血缘视图:目标表整合 + 角色 + 人话观察 + 风险。挂载于批量 report。"""

    targets: list[TargetIntegration] = Field(default_factory=list)
    table_roles: list[TableRole] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)
    risks: list[SemanticRisk] = Field(default_factory=list)


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
