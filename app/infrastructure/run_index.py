"""run_index 反向索引写入共享辅助。

compare_run 的"占位行"登记逻辑由两条入队路径共用:

* API 直连(``core.run_compare_task``)—— 从 compare_task 铸造一次性 run。
* worker workflow 子 job(``WorkerRunner._register_compare_run_child``)—— C-7 参数化
  compare 工作流 / C-8 trace_compare 生成的 compare_run 节点在入队后补登记。

两侧共用本函数以保证 status/bucket_counts/result_path 等 bookkeeping 一字不差,
避免复制粘贴漂移(见任务书:抽取 ``_enqueue_compare_run_job`` 既有登记逻辑)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import insert
from sqlalchemy.engine import Connection

from app.db.models import run_index
from app.domain.compare_result import empty_bucket_counts
from app.domain.job import JobKind, JobStatus


def register_compare_run_index(
    conn: Connection,
    *,
    run_id: str,
    job_id: str,
    owner_user_id: str,
    project_id: str,
    task_id: str | None,
    bucket_spools: dict[str, str],
) -> None:
    """在 ``run_index`` 插入 compare_run 的 pending 占位行。

    必须在对应 ``jobs`` 行已存在的事务里/之后调用(``run_index.job_id`` 外键 →
    ``jobs.id``)。``task_id`` 为可空外键(→ ``compare_tasks.id``,ON DELETE SET NULL):
    workflow 内联配置节点无 task 归属时传 ``None``。
    """
    now = datetime.now(UTC)
    conn.execute(
        insert(run_index).values(
            run_id=run_id,
            kind=JobKind.COMPARE_RUN.value,
            job_id=job_id,
            owner_user_id=owner_user_id,
            project_id=project_id,
            task_id=task_id,
            status=JobStatus.PENDING.value,
            result_path=f"compare/{run_id}",
            bucket_spools=bucket_spools,
            bucket_counts=empty_bucket_counts(),
            progress={},
            created_at=now,
            updated_at=now,
        )
    )
