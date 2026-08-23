"""Session Broker A1 schema/domain contract tests."""

from __future__ import annotations

from dataclasses import fields, is_dataclass

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, String, Table, UniqueConstraint

from app.db.models import console_sessions, console_statement_events, console_statements
from app.domain.console_session import (
    ACTIVE_CONSOLE_SESSION_STATES,
    ConsoleSession,
    ConsoleSessionState,
    ConsoleStatement,
    ConsoleStatementEvent,
    ConsoleStatementKind,
    ConsoleStatementState,
    ServerCancelState,
)


def _foreign_key_delete_rules(table: Table) -> dict[tuple[str, ...], str | None]:
    return {
        tuple(constraint.column_keys): constraint.ondelete
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }


def test_console_sessions_schema_matches_design() -> None:
    assert set(console_sessions.columns.keys()) == {
        "id",
        "console_id",
        "datasource_id",
        "owner_user_id",
        "epoch",
        "state",
        "broker_boot_id",
        "db_session_marker",
        "server_cancel",
        "autocommit",
        "created_at",
        "last_activity_at",
        "closed_at",
        "close_reason",
        "error_code",
    }
    assert _foreign_key_delete_rules(console_sessions) == {
        ("console_id",): "CASCADE",
        ("datasource_id",): "RESTRICT",
        ("owner_user_id",): "RESTRICT",
    }

    checks = {
        constraint.name
        for constraint in console_sessions.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert checks == {"ck_console_sessions_state_is_supported"}

    indexes = {str(index.name): index for index in console_sessions.indexes}
    active_index = indexes["uq_console_active_session"]
    assert active_index.unique is True
    assert [column.name for column in active_index.columns] == ["console_id"]
    assert "state IN" in str(active_index.dialect_options["postgresql"]["where"])
    assert indexes["ix_console_sessions_boot"].unique is False


def test_console_statements_schema_has_idempotency_key_and_history_fields() -> None:
    assert set(console_statements.columns.keys()) == {
        "id",
        "session_id",
        "console_id",
        "datasource_id",
        "owner_user_id",
        "epoch",
        "seq",
        "client_request_id",
        "sql_text",
        "sql_hash",
        "sql_len",
        "statement_kind",
        "is_write",
        "state",
        "cancel_requested",
        "result_set_id",
        "rows_affected",
        "error_code",
        "error_summary",
        "timeout_seconds",
        "script_id",
        "script_seq",
        "resolved_by",
        "resolved_at",
        "resolution",
        "submitted_at",
        "started_at",
        "finished_at",
    }
    assert _foreign_key_delete_rules(console_statements) == {
        ("session_id",): "CASCADE",
        ("result_set_id",): "SET NULL",
    }

    unique_constraints = {
        constraint.name: tuple(constraint.columns.keys())
        for constraint in console_statements.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert unique_constraints["uq_console_statements_session_request"] == (
        "session_id",
        "client_request_id",
    )
    checks = {
        constraint.name
        for constraint in console_statements.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert checks == {"ck_console_statements_state_is_supported"}
    sql_hash_type = console_statements.c.sql_hash.type
    assert isinstance(sql_hash_type, String)
    assert sql_hash_type.length == len("sha256:") + 64


def test_console_statement_events_follows_job_event_shape() -> None:
    assert set(console_statement_events.columns.keys()) == {
        "id",
        "statement_id",
        "ts",
        "event_type",
        "message",
        "detail",
    }
    assert _foreign_key_delete_rules(console_statement_events) == {("statement_id",): "CASCADE"}
    assert {index.name for index in console_statement_events.indexes} == {
        "ix_console_statement_events_statement_ts"
    }


def test_console_session_domain_snapshots_cover_schema() -> None:
    assert is_dataclass(ConsoleSession)
    assert is_dataclass(ConsoleStatement)
    assert is_dataclass(ConsoleStatementEvent)
    assert {field.name for field in fields(ConsoleSession)} == set(console_sessions.columns.keys())
    assert {field.name for field in fields(ConsoleStatement)} == set(
        console_statements.columns.keys()
    )
    assert {field.name for field in fields(ConsoleStatementEvent)} == set(
        console_statement_events.columns.keys()
    )


def test_console_session_domain_enums_match_persisted_value_sets() -> None:
    assert {state.value for state in ConsoleSessionState} == {
        "connecting",
        "idle",
        "executing",
        "cancelling",
        "closing",
        "closed",
        "session_lost",
        "connect_failed",
    }
    assert {state.value for state in ACTIVE_CONSOLE_SESSION_STATES} == {
        "connecting",
        "idle",
        "executing",
        "cancelling",
        "closing",
    }
    assert {state.value for state in ConsoleStatementState} == {
        "accepted",
        "executing",
        "streaming",
        "succeeded",
        "failed",
        "cancelled",
        "timeout",
        "outcome_unknown",
        "skipped",
    }
    assert {kind.value for kind in ConsoleStatementKind} == {
        "select",
        "dml",
        "ddl",
        "plsql",
        "other",
    }
    assert {state.value for state in ServerCancelState} == {
        "available",
        "degraded",
        "unknown",
    }
