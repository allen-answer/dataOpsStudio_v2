from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.schemas import CompareDataRef, CompareTaskCreateRequest


def test_result_snapshot_ref_requires_input_id() -> None:
    with pytest.raises(ValidationError, match="result_snapshot ref requires input_id"):
        CompareDataRef(kind="result_snapshot")


def test_result_snapshot_side_needs_no_datasource() -> None:
    request = CompareTaskCreateRequest(
        project_id="project-1",
        name="snapshot vs table",
        target_id="ds-target",
        source_ref=CompareDataRef(
            kind="result_snapshot",
            input_id="input-1",
        ),
        target_ref=CompareDataRef(kind="table", table_name="target_table"),
    )

    assert request.source_id is None
    assert request.source_ref.model_dump(exclude_none=True) == {
        "kind": "result_snapshot",
        "header_row": 1,
        "input_id": "input-1",
        "allow_partial": False,
    }


def test_result_snapshot_side_rejects_stale_datasource() -> None:
    with pytest.raises(ValidationError, match="source result_snapshot must not define source_id"):
        CompareTaskCreateRequest(
            project_id="project-1",
            name="invalid snapshot source",
            source_id="ds-stale",
            target_id="ds-target",
            source_ref=CompareDataRef(kind="result_snapshot", input_id="input-1"),
            target_ref=CompareDataRef(kind="table", table_name="target_table"),
        )
