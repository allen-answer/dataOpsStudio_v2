from __future__ import annotations

from app.dbclients.query_timing import QueryTimingRecorder


def test_empty_fetch_does_not_report_a_first_row() -> None:
    timing = QueryTimingRecorder()
    timing.reset()
    timing.mark_connect_started()
    timing.record_connect(5)
    timing.mark_execute_started()
    timing.record_execute(20)

    timing.record_fetch(7, row_count=0)

    snapshot = timing.execution_snapshot()
    assert snapshot["execute_first_row_ms"] == 0
    assert snapshot["first_row_at"] is None
    assert snapshot["fetch_ms"] == 7
    assert snapshot["connect_started_at"] is not None
    assert snapshot["connected_at"] is not None
    assert snapshot["execute_started_at"] is not None


def test_first_non_empty_fetch_records_first_row_once() -> None:
    timing = QueryTimingRecorder()
    timing.reset()
    timing.mark_execute_started()
    timing.record_execute(20)

    timing.record_fetch(7, row_count=10)
    first_row_at = timing.execution_snapshot()["first_row_at"]
    timing.record_fetch(3, row_count=5)
    timing.record_fetch(2, row_count=0)

    snapshot = timing.execution_snapshot()
    assert snapshot["execute_first_row_ms"] == 27
    assert snapshot["first_row_at"] == first_row_at
    assert snapshot["fetch_ms"] == 5
