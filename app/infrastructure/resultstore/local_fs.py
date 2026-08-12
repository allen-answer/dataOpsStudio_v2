from __future__ import annotations

import base64
import json
import shutil
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as dt_time
from decimal import Decimal
from pathlib import Path
from typing import Any, BinaryIO, cast

import pyarrow as pa
import pyarrow.parquet as pq

from app.domain.result import Manifest, ResultRef
from app.domain.schema import Column, Row
from app.infrastructure.resultstore.protocol import ResultStore

_BACKEND = "local_fs"
_pq_read_table = cast(Any, pq.read_table)
_pq_write_table = cast(Any, pq.write_table)


class ResultStoreError(RuntimeError):
    """LocalFsResultStore 基础异常。"""


@dataclass
class _PartInfo:
    path: str
    rows: int
    bytes: int


@dataclass
class _SpoolManifest:
    result_set_id: str
    parts: list[_PartInfo] = field(default_factory=list)
    columns: list[Column] = field(default_factory=list)
    loaded_rows: int = 0
    data_bytes: int = 0
    truncated: bool = False
    has_more: bool = False
    pagination_mode: str | None = None
    pagination_reason: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class LocalFsResultStore(ResultStore):
    """本地文件 ResultStore,ResultSet spool 使用 parquet part 文件。

    单写者假设:同一个 result_set_id 由一个 worker 顺序 append_spool 写入。
    多 worker 并发写同一 spool 不是 2.0.0 目标,需要后续引入文件锁/事务清单。
    """

    def __init__(
        self,
        root: str | Path,
        *,
        spool_max_rows: int = 1_000_000,
        spool_max_bytes: int = 512 * 1024 * 1024,
        result_ttl_days: int = 7,
        sql_export_ttl_hours: int = 24,
        upload_ttl_hours: int = 72,
    ) -> None:
        if spool_max_rows <= 0:
            raise ValueError("spool_max_rows must be positive")
        if spool_max_bytes <= 0:
            raise ValueError("spool_max_bytes must be positive")
        if sql_export_ttl_hours <= 0:
            raise ValueError("sql_export_ttl_hours must be positive")
        if upload_ttl_hours <= 0:
            raise ValueError("upload_ttl_hours must be positive")
        self._root = Path(root)
        self._spool_max_rows = spool_max_rows
        self._spool_max_bytes = spool_max_bytes
        self._result_ttl_days = result_ttl_days
        self._sql_export_ttl_hours = sql_export_ttl_hours
        self._upload_ttl_hours = upload_ttl_hours
        self._root.mkdir(parents=True, exist_ok=True)
        self._resultsets_dir.mkdir(parents=True, exist_ok=True)
        self._runs_dir.mkdir(parents=True, exist_ok=True)
        self._exports_dir.mkdir(parents=True, exist_ok=True)
        self._uploads_dir.mkdir(parents=True, exist_ok=True)

    def put_artifact(self, run_id: str, name: str, stream: BinaryIO) -> ResultRef:
        safe_name = _safe_artifact_name(name)
        artifact_dir = self._run_artifacts_dir(run_id)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        target = artifact_dir / safe_name
        with target.open("wb") as out:
            shutil.copyfileobj(stream, out)

        ref = self._ref_for_path(target)
        manifest = self._read_run_manifest(run_id)
        manifest.artifacts.append(ref)
        self._write_run_manifest(manifest)
        return ref

    def put_export_artifact(self, export_id: str, name: str, stream: BinaryIO) -> ResultRef:
        safe_name = _safe_artifact_name(name)
        export_dir = self._export_dir(export_id)
        export_dir.mkdir(parents=True, exist_ok=True)
        target = export_dir / safe_name
        with target.open("wb") as out:
            shutil.copyfileobj(stream, out)
        return self._ref_for_path(target)

    def put_upload_artifact(self, upload_id: str, name: str, stream: BinaryIO) -> ResultRef:
        """用户上传文件(血缘批量 ZIP / 对比文件源),由 upload_ttl_hours 清理。"""
        safe_name = _safe_artifact_name(name)
        upload_dir = self._upload_dir(upload_id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        target = upload_dir / safe_name
        with target.open("wb") as out:
            shutil.copyfileobj(stream, out)
        return self._ref_for_path(target)

    def delete_upload(self, upload_id: str) -> bool:
        path = self._upload_dir(upload_id)
        if not path.exists():
            return False
        shutil.rmtree(path, ignore_errors=True)
        return True

    def append_spool(self, result_set_id: str, rows: list[Row]) -> None:
        if not rows:
            return

        manifest = self._read_spool_manifest(result_set_id)
        if manifest.truncated:
            return

        remaining_rows = self._spool_max_rows - manifest.loaded_rows
        if remaining_rows <= 0:
            manifest.truncated = True
            self._write_spool_manifest(manifest)
            return

        candidate_rows = rows[:remaining_rows]
        if len(candidate_rows) < len(rows):
            manifest.truncated = True

        remaining_bytes = self._spool_max_bytes - manifest.data_bytes
        if remaining_bytes <= 0:
            manifest.truncated = True
            self._write_spool_manifest(manifest)
            return

        part_index = len(manifest.parts) + 1
        part_path = self._spool_parts_dir(result_set_id) / f"part-{part_index:06d}.parquet"
        rows_to_write = self._fit_rows_to_remaining_bytes(
            candidate_rows,
            part_path,
            remaining_bytes,
        )
        if not rows_to_write:
            manifest.truncated = True
            self._write_spool_manifest(manifest)
            return

        part_bytes = part_path.stat().st_size
        if len(rows_to_write) < len(candidate_rows):
            manifest.truncated = True

        manifest.parts.append(
            _PartInfo(
                path=str(part_path.relative_to(self._root)),
                rows=len(rows_to_write),
                bytes=part_bytes,
            )
        )
        manifest.loaded_rows += len(rows_to_write)
        manifest.data_bytes += part_bytes
        manifest.updated_at = time.time()
        if (
            manifest.loaded_rows >= self._spool_max_rows
            or manifest.data_bytes >= self._spool_max_bytes
        ):
            manifest.truncated = True
        self._write_spool_manifest(manifest)

    def mark_spool_truncated(self, result_set_id: str) -> None:
        manifest = self._read_spool_manifest(result_set_id)
        if manifest.truncated and manifest.has_more:
            return
        manifest.truncated = True
        manifest.has_more = True
        manifest.updated_at = time.time()
        self._write_spool_manifest(manifest)

    def set_spool_pagination(
        self,
        result_set_id: str,
        *,
        has_more: bool,
        mode: str,
        reason: str | None = None,
    ) -> None:
        manifest = self._read_spool_manifest(result_set_id)
        manifest.has_more = has_more or manifest.truncated
        manifest.pagination_mode = mode
        manifest.pagination_reason = reason
        manifest.updated_at = time.time()
        self._write_spool_manifest(manifest)

    def set_spool_columns(self, result_set_id: str, columns: list[Column]) -> None:
        manifest = self._read_spool_manifest(result_set_id)
        manifest.columns = [Column.model_validate(column.model_dump()) for column in columns]
        manifest.updated_at = time.time()
        self._write_spool_manifest(manifest)

    def get_manifest(self, run_id: str) -> Manifest:
        return self._read_run_manifest(run_id)

    def fetch_range(self, result_set_id: str, offset: int, limit: int) -> list[Row]:
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if limit < 0:
            raise ValueError("limit must be non-negative")
        if limit == 0:
            return []

        manifest = self._read_spool_manifest(result_set_id)
        wanted_start = offset
        wanted_end = offset + limit
        current_start = 0
        out: list[Row] = []

        for part in manifest.parts:
            current_end = current_start + part.rows
            if current_end <= wanted_start:
                current_start = current_end
                continue
            if current_start >= wanted_end:
                break

            local_start = max(wanted_start - current_start, 0)
            local_end = min(wanted_end - current_start, part.rows)
            out.extend(
                self._read_part_rows(self._root / part.path, local_start, local_end - local_start)
            )
            current_start = current_end
            if len(out) >= limit:
                break

        return out[:limit]

    def open_download(self, ref: ResultRef) -> BinaryIO:
        path = self._path_from_ref(ref)
        return path.open("rb")

    def delete_run(self, run_id: str) -> None:
        shutil.rmtree(self._run_dir(run_id), ignore_errors=True)

    def delete_spool(self, result_set_id: str) -> bool:
        path = self._spool_dir(result_set_id)
        if not path.exists():
            return False
        shutil.rmtree(path)
        return True

    def gc_expired(self) -> int:
        removed = 0
        if self._result_ttl_days >= 0:
            cutoff = time.time() - (self._result_ttl_days * 24 * 60 * 60)
            removed += self._gc_children_older_than(self._runs_dir, cutoff)
            removed += self._gc_children_older_than(self._resultsets_dir, cutoff)
        export_cutoff = time.time() - (self._sql_export_ttl_hours * 60 * 60)
        removed += self._gc_children_older_than(self._exports_dir, export_cutoff)
        upload_cutoff = time.time() - (self._upload_ttl_hours * 60 * 60)
        removed += self._gc_children_older_than(self._uploads_dir, upload_cutoff)
        return removed

    def spool_ref(self, result_set_id: str) -> ResultRef:
        return self._ref_for_path(self._spool_manifest_path(result_set_id))

    def spool_exists(self, result_set_id: str) -> bool:
        return (self._resultsets_dir / _safe_segment(result_set_id) / "manifest.json").exists()

    def get_spool_manifest(self, result_set_id: str) -> dict[str, Any]:
        return _spool_manifest_to_dict(self._read_spool_manifest(result_set_id))

    @property
    def _runs_dir(self) -> Path:
        return self._root / "runs"

    @property
    def _resultsets_dir(self) -> Path:
        return self._root / "resultsets"

    @property
    def _exports_dir(self) -> Path:
        return self._root / "exports"

    @property
    def _uploads_dir(self) -> Path:
        return self._root / "uploads"

    def _upload_dir(self, upload_id: str) -> Path:
        return self._uploads_dir / _safe_segment(upload_id)

    def _run_dir(self, run_id: str) -> Path:
        return self._runs_dir / _safe_segment(run_id)

    def _run_artifacts_dir(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "artifacts"

    def _export_dir(self, export_id: str) -> Path:
        return self._exports_dir / _safe_segment(export_id)

    def _run_manifest_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "manifest.json"

    def _spool_dir(self, result_set_id: str) -> Path:
        return self._resultsets_dir / _safe_segment(result_set_id)

    def _spool_parts_dir(self, result_set_id: str) -> Path:
        path = self._spool_dir(result_set_id) / "parts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _spool_manifest_path(self, result_set_id: str) -> Path:
        self._spool_dir(result_set_id).mkdir(parents=True, exist_ok=True)
        return self._spool_dir(result_set_id) / "manifest.json"

    def _read_run_manifest(self, run_id: str) -> Manifest:
        path = self._run_manifest_path(run_id)
        if not path.exists():
            return Manifest(run_id=run_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        return Manifest.model_validate(payload)

    def _write_run_manifest(self, manifest: Manifest) -> None:
        path = self._run_manifest_path(manifest.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

    def _read_spool_manifest(self, result_set_id: str) -> _SpoolManifest:
        path = self._spool_manifest_path(result_set_id)
        if not path.exists():
            return _SpoolManifest(result_set_id=result_set_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _spool_manifest_from_dict(payload)

    def _write_spool_manifest(self, manifest: _SpoolManifest) -> None:
        path = self._spool_manifest_path(manifest.result_set_id)
        temporary_path = path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(_spool_manifest_to_dict(manifest), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(path)

    def _gc_children_older_than(self, base: Path, cutoff: float) -> int:
        if not base.exists():
            return 0
        removed = 0
        for child in base.iterdir():
            if child.stat().st_mtime >= cutoff:
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
            removed += 1
        return removed

    def _fit_rows_to_remaining_bytes(
        self,
        rows: list[Row],
        part_path: Path,
        remaining_bytes: int,
    ) -> list[Row]:
        low = 0
        high = len(rows)
        best = 0
        while low <= high:
            mid = (low + high) // 2
            if mid == 0:
                low = 1
                continue
            tmp_path = part_path.with_suffix(".tmp")
            _write_rows_parquet(rows[:mid], tmp_path)
            size = tmp_path.stat().st_size
            tmp_path.unlink()
            if size <= remaining_bytes:
                best = mid
                low = mid + 1
            else:
                high = mid - 1

        if best == 0:
            return []
        _write_rows_parquet(rows[:best], part_path)
        return rows[:best]

    def _read_part_rows(self, path: Path, offset: int, limit: int) -> list[Row]:
        table = _pq_read_table(path, columns=["row_json"])
        sliced = table.slice(offset, limit)
        values = sliced.column("row_json").to_pylist()
        return [Row(values=_decode_row_json(item)) for item in values]

    def _ref_for_path(self, path: Path) -> ResultRef:
        return ResultRef(backend=_BACKEND, uri=path.relative_to(self._root).as_posix())

    def _path_from_ref(self, ref: ResultRef) -> Path:
        if ref.backend != _BACKEND:
            raise ResultStoreError(f"Unsupported result backend: {ref.backend}")
        path = (self._root / ref.uri).resolve()
        root = self._root.resolve()
        if root != path and root not in path.parents:
            raise ResultStoreError("ResultRef escapes LocalFsResultStore root")
        if not path.exists():
            raise FileNotFoundError(path)
        return path


def _write_rows_parquet(rows: list[Row], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row_json = [_encode_row_json(row) for row in rows]
    table = pa.table({"row_json": pa.array(row_json, type=pa.string())})
    _pq_write_table(table, path)


def _encode_row_json(row: Row) -> str:
    return json.dumps([_encode_value(value) for value in row.values], ensure_ascii=False)


def _decode_row_json(value: str) -> list[Any]:
    payload = json.loads(value)
    if not isinstance(payload, list):
        raise ResultStoreError("Invalid spool row payload")
    return [_decode_value(item) for item in payload]


def _encode_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {
            "__dataops_type__": "bytes_b64",
            "value": base64.b64encode(value).decode("ascii"),
        }
    if isinstance(value, Decimal):
        return {"__dataops_type__": "decimal", "value": str(value)}
    if isinstance(value, datetime):
        return {"__dataops_type__": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"__dataops_type__": "date", "value": value.isoformat()}
    if isinstance(value, dt_time):
        return {"__dataops_type__": "time", "value": value.isoformat()}
    return {"__dataops_type__": "repr", "value": str(value)}


def _decode_value(value: Any) -> Any:
    if not isinstance(value, dict) or "__dataops_type__" not in value:
        return value
    kind = value["__dataops_type__"]
    raw = value["value"]
    if kind == "bytes_b64":
        return base64.b64decode(raw.encode("ascii"))
    if kind == "decimal":
        return Decimal(str(raw))
    if kind == "datetime":
        return datetime.fromisoformat(str(raw))
    if kind == "date":
        return date.fromisoformat(str(raw))
    if kind == "time":
        return dt_time.fromisoformat(str(raw))
    return raw


def _spool_manifest_to_dict(manifest: _SpoolManifest) -> dict[str, Any]:
    return {
        "result_set_id": manifest.result_set_id,
        "parts": [part.__dict__ for part in manifest.parts],
        "columns": [column.model_dump() for column in manifest.columns],
        "loaded_rows": manifest.loaded_rows,
        "data_bytes": manifest.data_bytes,
        "truncated": manifest.truncated,
        "has_more": manifest.has_more or manifest.truncated,
        "pagination_mode": manifest.pagination_mode,
        "pagination_reason": manifest.pagination_reason,
        "created_at": manifest.created_at,
        "updated_at": manifest.updated_at,
    }


def _spool_manifest_from_dict(payload: dict[str, Any]) -> _SpoolManifest:
    return _SpoolManifest(
        result_set_id=str(payload["result_set_id"]),
        parts=[_PartInfo(**part) for part in payload.get("parts", [])],
        columns=[Column.model_validate(column) for column in payload.get("columns", [])],
        loaded_rows=int(payload.get("loaded_rows", 0)),
        data_bytes=int(payload.get("data_bytes", 0)),
        truncated=bool(payload.get("truncated", False)),
        has_more=bool(payload.get("has_more", payload.get("truncated", False))),
        pagination_mode=(
            str(payload["pagination_mode"])
            if isinstance(payload.get("pagination_mode"), str)
            else None
        ),
        pagination_reason=(
            str(payload["pagination_reason"])
            if isinstance(payload.get("pagination_reason"), str)
            else None
        ),
        created_at=float(payload.get("created_at", time.time())),
        updated_at=float(payload.get("updated_at", time.time())),
    )


def _safe_segment(value: str) -> str:
    if not value or "/" in value or "\\" in value or value in {".", ".."}:
        raise ValueError("Invalid path segment")
    return value


def _safe_artifact_name(value: str) -> str:
    name = Path(value).name
    if not name or name in {".", ".."} or name != value:
        raise ValueError("Invalid artifact name")
    return name


__all__ = ["LocalFsResultStore", "ResultStoreError"]
