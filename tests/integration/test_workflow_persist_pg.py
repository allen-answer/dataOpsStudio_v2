"""Workflow 2.4 PR-2 持久化往返:dag_jsonb ↔ WorkflowSpec(env-gate,无 PG 则 skip)。"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import Engine

from app.db.models import metadata, projects, users, workflow_templates, workflows
from app.domain.workflow import WorkflowSpec

pytestmark = pytest.mark.integration


def test_workflow_dag_jsonb_roundtrips_workflow_spec() -> None:
    engine = _pg_engine_or_skip()
    metadata.create_all(engine)
    _clear_metadata(engine)
    try:
        user_id, project_id = _seed_user_and_project(engine)
        spec = WorkflowSpec.model_validate(
            {
                "nodes": [
                    {
                        "id": "extract",
                        "job_kind": "sql_query",
                        "payload": {"sql": "SELECT 1"},
                        "timeout_seconds": 60,
                    },
                    {
                        "id": "export",
                        "job_kind": "export_excel",
                        "retry_policy": {"max_retries": 2, "backoff_seconds": 30},
                        "timeout_seconds": 120,
                        "on_failure": "continue",
                        "when": "nodes.extract.row_count > 0",
                    },
                ],
                "edges": [{"source": "extract", "target": "export"}],
                "schedule": {"cron": "0 6 * * 1-5", "enabled": True},
            }
        )
        workflow_id = str(uuid4())
        template_id = str(uuid4())
        assert spec.schedule is not None

        with engine.begin() as conn:
            conn.execute(
                insert(workflows).values(
                    id=workflow_id,
                    project_id=project_id,
                    name="daily-report",
                    dag_jsonb=spec.model_dump(mode="json"),
                    schedule_cron=spec.schedule.cron,
                    schedule_enabled=spec.schedule.enabled,
                    created_by=user_id,
                )
            )
            conn.execute(
                insert(workflow_templates).values(
                    id=template_id,
                    name="daily-report-template",
                    description="daily report DAG template",
                    dag_jsonb=spec.model_dump(mode="json"),
                    created_by=user_id,
                )
            )

        with engine.connect() as conn:
            wf_row = (
                conn.execute(select(workflows).where(workflows.c.id == workflow_id))
                .mappings()
                .one()
            )
            tpl_row = (
                conn.execute(
                    select(workflow_templates).where(workflow_templates.c.id == template_id)
                )
                .mappings()
                .one()
            )

        assert WorkflowSpec.model_validate(wf_row["dag_jsonb"]) == spec
        assert WorkflowSpec.model_validate(tpl_row["dag_jsonb"]) == spec
        assert wf_row["schedule_cron"] == "0 6 * * 1-5"
        assert wf_row["schedule_enabled"] is True
        assert wf_row["enabled"] is True  # server_default 总开关
    finally:
        _clear_metadata(engine)


def _seed_user_and_project(engine: Engine) -> tuple[str, str]:
    user_id = str(uuid4())
    project_id = str(uuid4())
    with engine.begin() as conn:
        conn.execute(
            insert(users).values(
                id=user_id,
                username="user-" + user_id,
                password_hash="not-used-in-this-test",
                role="admin",
            )
        )
        conn.execute(
            insert(projects).values(
                id=project_id,
                name="Project " + project_id,
                owner_user_id=user_id,
            )
        )
    return user_id, project_id


def _pg_engine_or_skip() -> Engine:
    url = os.environ.get("DATAOPS_TEST_PG_URL") or os.environ.get("DATAOPS_DATABASE_URL")
    if not url:
        pytest.skip("Postgres integration tests require DATAOPS_TEST_PG_URL")
    return create_engine(url, pool_size=10, max_overflow=20, pool_pre_ping=True)


def _clear_metadata(engine: Engine) -> None:
    with engine.begin() as conn:
        for table in reversed(metadata.sorted_tables):
            conn.execute(table.delete())
