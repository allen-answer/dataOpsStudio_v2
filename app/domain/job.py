from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, BaseModel, Field

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
    RESULT_EXPORT = "result_export"
    EXPORT_EXCEL = "export_excel"
    SCENARIO_MATERIALIZE = "scenario_materialize"
    SCENARIO_RUN_ALL = "scenario_run_all"
    WORKFLOW_RUN = "workflow_run"
    NOTIFY = "notify"
    SLEEP = "sleep"
    BRANCH = "branch"
    # C-10:sensor 数据到达检查(调度线程派发,worker 在 datasource 上跑只读 SQL,
    # 结果 truthy 则入队 workflow_run)。非 workflow 节点 kind(不在 ALLOWED_ 白名单)。
    WORKFLOW_SENSOR_CHECK = "workflow_sensor_check"
    AI_ASSIST_CALL = "ai_assist_call"
    AI_COPILOT_RUN = "ai_copilot_run"
    LINEAGE_ANALYZE = "lineage_analyze"
    LINEAGE_BATCH = "lineage_batch"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class JobErrorCode(StrEnum):
    CONNECTION_FAILED = "connection_failed"
    SQL_FAILED = "sql_failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    PERMISSION_DENIED = "permission_denied"
    UNSUPPORTED_DB_TYPE = "unsupported_db_type"
    EXPORT_FAILED = "export_failed"
    EXPORT_LIMIT_EXCEEDED = "export_limit_exceeded"
    # 文件源对比全量物化超行数硬顶(C-1;不静默截断,任务直接 failed)
    COMPARE_LIMIT_EXCEEDED = "compare_limit_exceeded"
    # 抽样快检要求单一整数主键;联合/非整数主键直接 failed,不再返回空结果假成功
    COMPARE_SAMPLE_UNSUPPORTED_KEY = "compare_sample_unsupported_key"
    INTERNAL = "internal"


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
    available_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
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
    error_code: JobErrorCode | None = None
    retry_count: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)
    parent_workflow_run_id: str | None = None
