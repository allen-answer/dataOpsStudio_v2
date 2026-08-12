from __future__ import annotations

from typing import BinaryIO, Protocol

from app.domain.result import Manifest, ResultRef
from app.domain.schema import Row


class ResultStore(Protocol):
    """Run-level artifact + ResultSet spool 存储(契约 §3.4、设计稿 §5.3)。

    2.0.0:LocalFsResultStore(parquet/arrow 落本地)。S3ResultStore 后置 hosted。

    ★ R6 红线相关:append_spool 配合 ResultSet 实现"边拉边看"——
    worker fetchmany → append_spool 流式追加,前端 fetch_range 读 spool offset/limit。
    DB cursor 秒级持有即关,**不被 UI idle 绑住**(契约 §3.4 / 设计稿 §2.6.3)。
    """

    def put_artifact(self, run_id: str, name: str, stream: BinaryIO) -> ResultRef:
        """一次性写入(如 export_excel 的 xlsx 文件)。"""
        raise NotImplementedError

    def put_export_artifact(self, export_id: str, name: str, stream: BinaryIO) -> ResultRef:
        """写入 SQL Workspace 导出文件,由 sql_export_ttl_hours 单独清理。"""
        raise NotImplementedError

    def put_upload_artifact(self, upload_id: str, name: str, stream: BinaryIO) -> ResultRef:
        """写入用户上传文件(血缘批量 ZIP / 对比文件源),由 upload_ttl_hours 清理。"""
        raise NotImplementedError

    def delete_upload(self, upload_id: str) -> bool:
        """删除一个上传(TTL GC 之外的显式即时释放)。"""
        raise NotImplementedError

    def append_spool(self, result_set_id: str, rows: list[Row]) -> None:
        """流式追加 spool(配合 ResultSet 实现 streaming)。"""
        raise NotImplementedError

    def mark_spool_truncated(self, result_set_id: str) -> None:
        """幂等标记 spool 已截断,供查询主动限行后关闭数据库游标。"""
        raise NotImplementedError

    def set_spool_pagination(
        self,
        result_set_id: str,
        *,
        has_more: bool,
        mode: str,
        reason: str | None = None,
    ) -> None:
        """记录无 COUNT 分页状态;不保存 cursor、连接或业务行值。"""
        raise NotImplementedError

    def get_manifest(self, run_id: str) -> Manifest:
        raise NotImplementedError

    def fetch_range(self, result_set_id: str, offset: int, limit: int) -> list[Row]:
        """从 spool 读 [offset, offset+limit);ResultSet.fetch_range 委托到这里。"""
        raise NotImplementedError

    def spool_exists(self, result_set_id: str) -> bool:
        """返回 ResultSet spool manifest 是否仍存在。"""
        raise NotImplementedError

    def get_spool_manifest(self, result_set_id: str) -> dict[str, object]:
        """读取 ResultSet spool manifest。"""
        raise NotImplementedError

    def open_download(self, ref: ResultRef) -> BinaryIO:
        raise NotImplementedError

    def delete_run(self, run_id: str) -> None:
        raise NotImplementedError

    def delete_spool(self, result_set_id: str) -> bool:
        """删除一个 ResultSet spool,返回是否实际删除了文件。"""
        raise NotImplementedError

    def gc_expired(self) -> int:
        """按 result_ttl_days / resultset_spool_ttl_days / sql_export_ttl_hours 清理。"""
        raise NotImplementedError
