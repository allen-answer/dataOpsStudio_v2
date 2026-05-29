from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.domain.resource import ResourceProfile
from app.domain.result import ResultRef


class JobKind(StrEnum):
    """统一 Job 类型(契约 §3.6、设计稿 §2.5)。

    1.x 痛点:5 套 execution 并存,cancel/retry/owner/audit 各搞各的。
    2.0 统一:所有长任务都是 Job,kind 字段区分类型。

    注意 workflow 节点白名单 ALLOWED_WORKFLOW_NODE_KINDS 是另一集合
    (含 notify/sleep/branch,不含 workflow_run/ai_assist_call),勿混淆。
    """

    SQL_QUERY = "sql_query"
    TEST_CONNECTION = "test_connection"
    SQL_EXPLAIN = "sql_explain"
    COMPARE_RUN = "compare_run"
    EXPORT_EXCEL = "export_excel"
    SCENARIO_MATERIALIZE = "scenario_materialize"
    SCENARIO_RUN_ALL = "scenario_run_all"
    WORKFLOW_RUN = "workflow_run"
    AI_ASSIST_CALL = "ai_assist_call"
    AI_COPILOT_RUN = "ai_copilot_run"
    LINEAGE_ANALYZE = "lineage_analyze"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


# Workflow 节点白名单(R7 红线;契约 §4)。
# 节点 kind 与 Job kind 部分重叠但不相等:workflow 节点含 notify/sleep/branch,
# 但不能起 workflow_run / ai_assist_call / ai_copilot_run(防嵌套 + AI 出站滥用)。
ALLOWED_WORKFLOW_NODE_KINDS: frozenset[str] = frozenset(
    {
        "sql_query",
        "sql_explain",
        "compare_run",
        "scenario_materialize",
        "scenario_run_all",
        "lineage_analyze",
        "export_excel",
        "notify",
        "sleep",
        "branch",
    }
)


class Job(BaseModel):
    """统一长任务对象(契约 §3.6、设计稿 §2.5)。

    持久化到 PG `jobs` 表,worker 通过 PG queue (FOR UPDATE SKIP LOCKED) 抢任务。
    """

    id: str
    kind: JobKind
    status: JobStatus
    owner_user_id: str
    project_id: str
    datasource_ids: list[str] = Field(default_factory=list)
    priority: int
    timeout_seconds: int
    resource_profile: ResourceProfile  # 2.0.0 字段最小集,见 app/domain/resource.py
    result_ref: ResultRef | None = None
    audit_id: str
    worker_id: str | None = None
    last_heartbeat: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested: bool = False
    cancel_reason: str | None = None
    error: str | None = None
    retry_count: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)
    parent_workflow_run_id: str | None = None
