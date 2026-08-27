"""导出文件名规范化与 Content-Disposition 安全编码。

任务名是用户自由输入,一旦拼进文件名就同时是**落盘路径**和**响应头内容**,
两侧都得收敛 —— 本文件守的主要是这个,而不只是「好看」。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy.engine import RowMapping

from app.api.routes.core import (
    _compare_export_filename,
    _content_disposition,
    _safe_export_stem,
)

# ── 词干消毒 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("dwd_ast_scr_acc_hold_030", "dwd_ast_scr_acc_hold_030"),
        ("交易流水", "交易流水"),  # 中文保留 —— 可读性正是本次目的
        ("has space", "has_space"),
        ("  padded  ", "padded"),
        ("tabs\tand\nnewlines", "tabs_and_newlines"),
    ],
)
def test_safe_export_stem_keeps_readable_names(raw: str, expected: str) -> None:
    assert _safe_export_stem(raw, fallback="compare") == expected


@pytest.mark.parametrize(
    "raw",
    [
        "../../etc/passwd",
        "..\\..\\windows\\system32",
        "a/b/c",
        'quote"inject',
        "cr\rlf\ninject",
        "null\x00byte",
    ],
)
def test_safe_export_stem_strips_path_and_header_injection(raw: str) -> None:
    """路径穿越与响应头注入字符必须消失。"""
    stem = _safe_export_stem(raw, fallback="compare")

    for bad in ("/", "\\", '"', "\r", "\n", "\x00"):
        assert bad not in stem, (raw, stem)


@pytest.mark.parametrize("raw", ["", "   ", "...", "___", None])
def test_safe_export_stem_falls_back_when_nothing_usable_remains(raw: str | None) -> None:
    assert _safe_export_stem(raw, fallback="compare") == "compare"


@pytest.mark.parametrize("raw", ["CON", "nul", "COM1", "lpt9"])
def test_safe_export_stem_rejects_windows_reserved_names(raw: str) -> None:
    """CON.xlsx 一类在 Windows 上根本存不下来。"""
    assert _safe_export_stem(raw, fallback="compare") == "compare"


def test_safe_export_stem_caps_length() -> None:
    stem = _safe_export_stem("x" * 500, fallback="compare")

    assert 0 < len(stem) <= 60


def test_safe_export_stem_never_ends_with_dot_or_space() -> None:
    """Windows 不允许文件名以点或空格结尾。"""
    for raw in ("name.", "name ", "name. . "):
        stem = _safe_export_stem(raw, fallback="compare")
        assert not stem.endswith((".", " ")), raw


# ── 完整文件名 ──────────────────────────────────────────────────────────────


def _run_row(run_id: str, created: datetime | None) -> RowMapping:
    """_compare_export_filename 只按键取值,dict 足以替身。"""
    row: dict[str, Any] = {"run_id": run_id, "created_at": created, "task_id": "t-1"}
    return cast(RowMapping, row)


def test_compare_export_filename_is_readable_and_traceable() -> None:
    row = _run_row(
        "d26a65e4-11c4-46f2-8514-04806f01b233",
        datetime(2026, 8, 27, 16, 50, 0, tzinfo=UTC),
    )

    name = _compare_export_filename(row, "dwd_ast_scr_acc_hold_030")

    # 任务名可读 + 时间戳可排序 + run 短码可追回
    assert name == "dwd_ast_scr_acc_hold_030_20260827-165000_d26a65e4.xlsx"


def test_compare_export_filename_handles_missing_task_name() -> None:
    row = _run_row(
        "abcdef12-0000-0000-0000-000000000000", datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    )

    name = _compare_export_filename(row, None)

    assert name.startswith("compare_20260102-030405_abcdef12")
    assert name.endswith(".xlsx")


def test_compare_export_filename_sanitizes_hostile_task_name() -> None:
    row = _run_row("11112222-0000-0000-0000-000000000000", datetime(2026, 1, 1, tzinfo=UTC))

    name = _compare_export_filename(row, '../../evil"\r\nSet-Cookie: x=1')

    for bad in ("/", "\\", '"', "\r", "\n"):
        assert bad not in name, name
    assert name.endswith("_11112222.xlsx")


# ── Content-Disposition ─────────────────────────────────────────────────────


def test_content_disposition_carries_ascii_fallback_and_utf8() -> None:
    header = _content_disposition("交易流水_20260827-165000_abcd1234.xlsx")

    assert header.startswith("attachment; ")
    assert "filename=" in header
    assert "filename*=UTF-8''" in header
    # 中文经百分号编码后进 filename*
    assert "%E4%BA%A4%E6%98%93" in header


def test_content_disposition_cannot_be_broken_by_quotes_or_crlf() -> None:
    """即便 filename 列里存了敌意值(历史数据/其它写入方),响应头也不能被截断。"""
    header = _content_disposition('evil".xlsx\r\nSet-Cookie: a=b')

    assert "\r" not in header
    assert "\n" not in header
    # ASCII 回退部分不得出现裸引号
    fallback = header.split('filename="', 1)[1].split('"', 1)[0]
    assert '"' not in fallback


def test_content_disposition_falls_back_when_name_is_all_non_ascii() -> None:
    header = _content_disposition("交易流水.xlsx")

    assert 'filename="' in header
    assert "filename*=UTF-8''" in header
