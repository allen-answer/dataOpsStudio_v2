"""语句结果落盘缝(Session Broker 设计 §3.2 spool 复用 / §10-A5 平价)。

**为什么是一条缝而不是直接调 resultstore**:`app/broker/core.py` 的 lane 只认
协议(与 `BrokerStore` 同型),这样状态机单测不需要真文件系统与真 PG;生产装配
在 `app/broker/wiring.py`。

**平价口径**(与 job 路径逐条对齐,避免两套结果契约漂移):

- result_set_id 在 **submit 时**分配并落 `result_sets` 占位行(job 路径同型:
  `core.py` `_create_result_set_placeholder`)—— 前端拿到 submit 回执即可开轮询;
- 落 spool 走 `LocalFsResultStore.append_spool`(#195 快路径),`result_version`
  单调递增即前端增量拉取判据,#188/#211 的渐进分页与虚拟化资产原样可用;
- 同 console 结果集淘汰 3/console:**与 job 路径同一条策略**
  (`max_active_resultsets_per_console`,同一份配置项、同一条 SQL 口径)。
  这里没有复用 `app/api/routes/core.py` 的那份实现 —— 让 `app/broker/`
  import API 路由模块是反向依赖;两处各自的 3/console 行为由契约测试钉住;
- 终态把 `timings/execution` 写进 `result_sets.storage_ref.metadata`,喂
  progress 的 `JobProgressResponse` 镜像字段(job 路径放在 `jobs.result_ref`)。

**刻意的行为偏离**(设计 §2.2 / §11-7):`cancelled` / `timeout` 的语句
**保留已落 spool 的部分行**,不像 job 路径那样取消即删。因此 `finalize()`
对任何终态都只封存、不删除。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection, Engine

from app.db.models import result_sets
from app.domain.console_session import ConsoleStatement
from app.domain.result import ResultRef
from app.domain.schema import Column, Row


@dataclass(frozen=True, slots=True)
class StatementSpool:
    """一条语句的结果集句柄。lane 持有,生命周期 = 语句执行期 + 事后读页。"""

    result_set_id: str
    # 单批取数与 spool 落盘的批大小(submit 参数快照)。
    page_size: int
    # 客户端截断上限(DataGrip 语义:取够就停,连接与会话续用)。
    max_result_rows: int


@dataclass(slots=True)
class StatementMetrics:
    """`JobExecutionMetrics` 镜像所需的、lane 真正知道的量。

    会话路径没有的字段一律留 None,**不编默认值** —— 免握手(无 per-statement
    connect)正是会话模型的收益,把 connect_ms 填 0 会让性能对比读出假数据。
    """

    execute_started_at: datetime | None = None
    first_row_at: datetime | None = None
    finished_reading_at: datetime | None = None
    rows_read: int = 0
    rows_returned: int = 0
    execute_to_first_row_ms: int | None = None
    fetch_ms: int = 0
    spool_ms: int = 0
    db_type: str | None = None
    effective_sql_hash: str | None = None
    output_limit_applied: bool = False

    def to_metadata(self, spool: StatementSpool) -> dict[str, object]:
        return {
            "timings": {
                "execute_first_row_ms": self.execute_to_first_row_ms,
                "fetch_ms": self.fetch_ms,
                "spool_ms": self.spool_ms,
            },
            "execution": {
                "execute_started_at": _iso(self.execute_started_at),
                "first_row_at": _iso(self.first_row_at),
                "finished_reading_at": _iso(self.finished_reading_at),
                "rows_read": self.rows_read,
                "rows_returned": self.rows_returned,
                "max_rows": spool.max_result_rows,
                "page_size": spool.page_size,
                # 会话路径不改写 SQL(不做 LIMIT 下推),截断是客户端取够就停。
                "limit_pushdown": False,
                "limit_pushdown_reason": "session_client_side_cap",
                "output_limit_applied": self.output_limit_applied,
                "effective_sql_hash": self.effective_sql_hash,
                "db_type": self.db_type,
            },
        }


class StatementResults(Protocol):
    """lane 与结果存储之间的唯一接触面。"""

    def register(
        self,
        statement: ConsoleStatement,
        *,
        page_size: int,
        max_result_rows: int,
    ) -> StatementSpool:
        """分配 result_set_id 并落占位行 + 触发同 console 淘汰。"""
        ...

    def set_columns(self, spool: StatementSpool, columns: Sequence[Column]) -> None: ...

    def append(self, spool: StatementSpool, rows: Sequence[Row]) -> None: ...

    def mark_truncated(self, spool: StatementSpool) -> None: ...

    def loaded_rows(self, spool: StatementSpool) -> int: ...

    def publish_streaming(
        self,
        spool: StatementSpool,
        statement: ConsoleStatement,
        *,
        columns: Sequence[Column],
    ) -> None:
        """流式中途同步 catalog(前端 `state=streaming` + loaded_rows 的来源)。"""
        ...

    def finalize(
        self,
        spool: StatementSpool,
        statement: ConsoleStatement,
        *,
        columns: Sequence[Column],
        metrics: StatementMetrics,
    ) -> None:
        """封存结果集。终态是 cancelled/timeout 时**同样封存部分行**(§11-7)。"""
        ...


class NullStatementResults(StatementResults):
    """不落盘的替身:状态机单测与"只关心契约"的用例用。

    仍然分配 result_set_id 并记录批次,便于断言 lane 的落盘调用序,但不碰
    文件系统与 PG。**生产装配绝不能用它** —— `build_session_broker` 装的是
    `SpoolStatementResults`,`test_wiring` 钉死这一点。
    """

    def __init__(self) -> None:
        self.batches: dict[str, list[Row]] = {}
        self.columns: dict[str, list[Column]] = {}
        self.truncated: set[str] = set()
        self.finalized: dict[str, tuple[ConsoleStatement, StatementMetrics]] = {}
        self.registered: list[str] = []

    def register(
        self,
        statement: ConsoleStatement,
        *,
        page_size: int,
        max_result_rows: int,
    ) -> StatementSpool:
        result_set_id = str(uuid4())
        self.batches[result_set_id] = []
        self.columns[result_set_id] = []
        self.registered.append(result_set_id)
        return StatementSpool(
            result_set_id=result_set_id,
            page_size=page_size,
            max_result_rows=max_result_rows,
        )

    def set_columns(self, spool: StatementSpool, columns: Sequence[Column]) -> None:
        self.columns[spool.result_set_id] = list(columns)

    def append(self, spool: StatementSpool, rows: Sequence[Row]) -> None:
        self.batches[spool.result_set_id].extend(rows)

    def mark_truncated(self, spool: StatementSpool) -> None:
        self.truncated.add(spool.result_set_id)

    def loaded_rows(self, spool: StatementSpool) -> int:
        return len(self.batches[spool.result_set_id])

    def publish_streaming(
        self,
        spool: StatementSpool,
        statement: ConsoleStatement,
        *,
        columns: Sequence[Column],
    ) -> None:
        return None

    def finalize(
        self,
        spool: StatementSpool,
        statement: ConsoleStatement,
        *,
        columns: Sequence[Column],
        metrics: StatementMetrics,
    ) -> None:
        self.finalized[spool.result_set_id] = (statement, metrics)


class SpoolResultStore(Protocol):
    """`LocalFsResultStore` 里 lane 需要的那几个方法(结构化子集,便于替身)。"""

    def append_spool(self, result_set_id: str, rows: list[Row]) -> None: ...

    def set_spool_columns(self, result_set_id: str, columns: list[Column]) -> None: ...

    def mark_spool_truncated(self, result_set_id: str) -> None: ...

    def get_spool_manifest(self, result_set_id: str) -> dict[str, object]: ...

    def spool_ref(self, result_set_id: str) -> ResultRef: ...

    def delete_spool(self, result_set_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class _EvictionPolicy:
    max_active_per_console: int = 3


class SpoolStatementResults(StatementResults):
    """生产实现:PG `result_sets` catalog + `LocalFsResultStore` spool。

    单写者约束(`local_fs.py:56-58`)由"lane 是其 result_set 的唯一写者"保证 ——
    一个 result_set 只属于一条语句,一条语句只在自己的 lane 线程上跑。
    """

    def __init__(
        self,
        engine: Engine,
        result_store: SpoolResultStore,
        *,
        max_active_resultsets_per_console: int = 3,
    ) -> None:
        self._engine = engine
        self._store = result_store
        self._policy = _EvictionPolicy(max(1, max_active_resultsets_per_console))

    def register(
        self,
        statement: ConsoleStatement,
        *,
        page_size: int,
        max_result_rows: int,
    ) -> StatementSpool:
        result_set_id = str(uuid4())
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            conn.execute(
                insert(result_sets).values(
                    id=result_set_id,
                    execution_id=statement.id,
                    console_id=statement.console_id,
                    storage_ref=self._store.spool_ref(result_set_id).model_dump(),
                    columns=[],
                    loaded_rows=0,
                    total_rows=None,
                    state="streaming",
                    created_at=now,
                    updated_at=now,
                )
            )
        self._evict_console_resultsets(statement.console_id)
        return StatementSpool(
            result_set_id=result_set_id,
            page_size=page_size,
            max_result_rows=max_result_rows,
        )

    def set_columns(self, spool: StatementSpool, columns: Sequence[Column]) -> None:
        self._store.set_spool_columns(spool.result_set_id, list(columns))

    def append(self, spool: StatementSpool, rows: Sequence[Row]) -> None:
        self._store.append_spool(spool.result_set_id, list(rows))

    def mark_truncated(self, spool: StatementSpool) -> None:
        self._store.mark_spool_truncated(spool.result_set_id)

    def loaded_rows(self, spool: StatementSpool) -> int:
        return _manifest_int(self._store.get_spool_manifest(spool.result_set_id), "loaded_rows")

    def publish_streaming(
        self,
        spool: StatementSpool,
        statement: ConsoleStatement,
        *,
        columns: Sequence[Column],
    ) -> None:
        self._write_catalog(
            spool,
            statement,
            columns=columns,
            state="streaming",
            loaded_rows=self.loaded_rows(spool),
            total_rows=None,
            metadata=None,
        )

    def finalize(
        self,
        spool: StatementSpool,
        statement: ConsoleStatement,
        *,
        columns: Sequence[Column],
        metrics: StatementMetrics,
    ) -> None:
        manifest = self._store.get_spool_manifest(spool.result_set_id)
        loaded_rows = _manifest_int(manifest, "loaded_rows")
        truncated = bool(manifest.get("truncated"))
        self._write_catalog(
            spool,
            statement,
            columns=columns,
            state="complete",
            loaded_rows=loaded_rows,
            # 截断(或被取消)时行数不是全量,total_rows 只能是 null ——
            # 报一个"看起来完整"的总数就是撒谎。
            total_rows=None if truncated or metrics.output_limit_applied else loaded_rows,
            metadata=metrics.to_metadata(spool),
        )

    # ── internals ─────────────────────────────────────────────────────────

    def _write_catalog(
        self,
        spool: StatementSpool,
        statement: ConsoleStatement,
        *,
        columns: Sequence[Column],
        state: str,
        loaded_rows: int,
        total_rows: int | None,
        metadata: dict[str, object] | None,
    ) -> None:
        ref = self._store.spool_ref(spool.result_set_id)
        if metadata is not None:
            ref = ref.model_copy(update={"metadata": metadata})
        values: dict[str, object] = {
            "execution_id": statement.id,
            "console_id": statement.console_id,
            "storage_ref": ref.model_dump(),
            "columns": [column.model_dump() for column in columns],
            "loaded_rows": loaded_rows,
            "total_rows": total_rows,
            "state": state,
            "updated_at": datetime.now(UTC),
        }
        with self._engine.begin() as conn:
            conn.execute(
                update(result_sets).where(result_sets.c.id == spool.result_set_id).values(**values)
            )

    def _evict_console_resultsets(self, console_id: str) -> None:
        """同 console 只保留最近 N 个活跃结果集(job 路径 3/console 平价)。

        排序与 job 路径一致(updated_at desc):刚注册的这条在最前,跑着的语句
        不会把自己淘汰掉。被淘汰的行标 closed 并删 spool —— 与 job 路径同型。
        """

        with self._engine.begin() as conn:
            stale_ids = self._stale_console_resultsets(conn, console_id)
            if stale_ids:
                conn.execute(
                    update(result_sets)
                    .where(result_sets.c.id.in_(stale_ids))
                    .values(state="closed", updated_at=datetime.now(UTC))
                )
        for result_set_id in stale_ids:
            try:
                self._store.delete_spool(result_set_id)
            except Exception:
                # 删文件失败不该拖垮 submit:catalog 已标 closed,
                # 文件由 worker 的 TTL GC 兜底回收。
                continue

    def _stale_console_resultsets(self, conn: Connection, console_id: str) -> list[str]:
        active_ids = (
            conn.execute(
                select(result_sets.c.id)
                .where(result_sets.c.console_id == console_id)
                .where(result_sets.c.state != "closed")
                .order_by(result_sets.c.updated_at.desc(), result_sets.c.created_at.desc())
            )
            .scalars()
            .all()
        )
        return [str(value) for value in active_ids[self._policy.max_active_per_console :]]


def _manifest_int(manifest: dict[str, object], key: str) -> int:
    value = manifest.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = [
    "NullStatementResults",
    "SpoolResultStore",
    "SpoolStatementResults",
    "StatementMetrics",
    "StatementResults",
    "StatementSpool",
]
