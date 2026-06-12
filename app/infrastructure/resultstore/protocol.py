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

    def append_spool(self, result_set_id: str, rows: list[Row]) -> None:
        """流式追加 spool(配合 ResultSet 实现 streaming)。"""
        raise NotImplementedError

    def get_manifest(self, run_id: str) -> Manifest:
        raise NotImplementedError

    def fetch_range(self, result_set_id: str, offset: int, limit: int) -> list[Row]:
        """从 spool 读 [offset, offset+limit);ResultSet.fetch_range 委托到这里。"""
        raise NotImplementedError

    def open_download(self, ref: ResultRef) -> BinaryIO:
        raise NotImplementedError

    def delete_run(self, run_id: str) -> None:
        raise NotImplementedError

    def delete_spool(self, result_set_id: str) -> bool:
        """删除一个 ResultSet spool,返回是否实际删除了文件。"""
        raise NotImplementedError

    def gc_expired(self) -> int:
        """按 result_ttl_days / resultset_spool_ttl_days 清理,返回清理数量。"""
        raise NotImplementedError
