from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.schema import Column, Row

SQL_WORKSPACE_DEFAULT_PAGE_SIZE = 100
SQL_WORKSPACE_MAX_PAGE_SIZE = 1_000
SQL_WORKSPACE_DEFAULT_MAX_ROWS = 1_000
SQL_WORKSPACE_MAX_ROWS = 50_000


class ResultRef(BaseModel):
    """跨存储 artifact 的逻辑引用 —— 不是 PG 外键。

    backend 标识 store 类型(local_fs / s3 / ...),uri 是 store 内部寻址。
    LocalFsResultStore 用文件路径,S3ResultStore 用 s3 URL。
    """

    model_config = ConfigDict(frozen=True)

    backend: str  # "local_fs" / "s3" / ...
    uri: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class Manifest(BaseModel):
    """Run 级 artifact 清单(契约 §3.4 ResultStore.get_manifest 返回值)。"""

    run_id: str
    artifacts: list[ResultRef] = Field(default_factory=list)


class SpoolSnapshotManifest(BaseModel):
    """ResultSet spool 的不可变副本描述,不暴露底层 part 路径。"""

    model_config = ConfigDict(frozen=True)

    result_set_id: str
    columns: tuple[Column, ...] = ()
    loaded_rows: int = 0
    data_bytes: int = 0
    truncated: bool = False
    has_more: bool = False


ResultSetState = Literal["streaming", "complete", "failed", "closed"]


class ResultSet(BaseModel):
    """一次执行的结果。数据落本地 spool(parquet/arrow),**不持 DB cursor**。

    ★★ R6 红线:本类禁止持有 DbCursor / Cursor 字段。
    cursor 持有时长由 cursor_max_hold_seconds(配置)控制,
    DB 资源在 worker 拉取期间持有,拉完即关。UI 通过 storage_ref 读 spool。

    见设计稿 §2.6.2、契约 §3.4。Step 1.7 用 ast-grep 同时禁
    "cursor 字段名"和"DbCursor/Cursor 类型注解",防绕过。

    fetch_range / request_count_total 在 Step 2 由 ResultStore 实现注入,
    Step 1 只占位接口。
    """

    result_set_id: str
    execution_id: str
    storage_ref: ResultRef  # 指向 spool,不是 cursor
    columns: list[Column]
    loaded_rows: int = 0
    total_rows: int | None = None
    state: ResultSetState = "streaming"
    created_at: datetime

    def fetch_range(self, offset: int, limit: int) -> list[Row]:
        """从 spool 读 [offset, offset+limit) 范围的行。Step 2 由 ResultStore 实现。"""
        raise NotImplementedError

    def request_count_total(self) -> int:
        """用户主动触发的 total count(可能扫全 spool)。Step 2 实现。"""
        raise NotImplementedError
