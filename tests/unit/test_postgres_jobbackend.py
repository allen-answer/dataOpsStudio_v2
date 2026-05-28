from __future__ import annotations

from typing import cast

import pytest
from sqlalchemy.engine import Engine

from app.domain.result import ResultRef
from app.infrastructure.jobbackend.postgres import PostgresJobBackend


def test_complete_fail_require_constructor_worker_id() -> None:
    backend = PostgresJobBackend(cast(Engine, object()))

    with pytest.raises(RuntimeError, match="complete/fail requires worker_id"):
        backend.complete("job-1", ResultRef(backend="local_fs", uri="spool/job-1"))

    with pytest.raises(RuntimeError, match="complete/fail requires worker_id"):
        backend.fail("job-1", "boom")
