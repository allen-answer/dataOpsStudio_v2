from __future__ import annotations

from datetime import UTC
from typing import Any, cast

from sqlalchemy.engine import Engine

from app.api.services import ApiServices, RateLimiter


def test_rate_limiter_keeps_policy_groups_and_users_independent() -> None:
    limiter = RateLimiter()

    for index in range(300):
        assert limiter.check("user:user-1", group="job_read", now=float(index) / 10).allowed
    assert not limiter.check("user:user-1", group="job_read", now=30.0).allowed
    assert limiter.check("user:user-1", group="sql_control", now=30.0).allowed
    assert limiter.check("user:user-2", group="job_read", now=30.0).allowed


def test_rate_limiter_allows_long_adaptive_queries_but_blocks_malicious_burst() -> None:
    limiter = RateLimiter()

    # Two consoles polling every five seconds for ten minutes never exceed the
    # 300/minute job-read window.
    for second in range(0, 600, 5):
        assert limiter.check("user:operator", group="job_read", now=float(second)).allowed
        assert limiter.check("user:operator", group="job_read", now=float(second) + 0.01).allowed

    burst = RateLimiter()
    for index in range(300):
        assert burst.check("user:operator", group="job_read", now=float(index) / 1000).allowed
    decision = burst.check("user:operator", group="job_read", now=0.3)
    assert decision.allowed is False
    assert decision.retry_after_seconds > 59


def test_rate_limiter_explicit_limit_overrides_all_groups_for_test_fixtures() -> None:
    limiter = RateLimiter(limit=2, window_seconds=10)

    assert limiter.check("same", group="auth", now=0).allowed
    assert limiter.check("same", group="auth", now=1).allowed
    assert not limiter.check("same", group="auth", now=2).allowed
    assert limiter.check("same", group="job_read", now=2).allowed


def test_is_token_revoked_uses_read_connection_only() -> None:
    engine = _ReadOnlyEngine([[{"tokens_revoked_after": None}]])
    services = _services(engine)

    assert (
        services.is_token_revoked(
            user_id="user-1",
            issued_at=100,
            expires_at=200,
            jti=None,
        )
        is False
    )

    assert engine.connect_count == 1
    assert engine.begin_count == 0
    assert [statement for statement in engine.statements] == ["select"]


def test_revoke_user_tokens_prunes_expired_jti_rows_in_write_transaction() -> None:
    engine = _WriteEngine()
    services = _services(engine)

    revoked_after = services.revoke_user_tokens(user_id="user-1")

    assert revoked_after.tzinfo is UTC
    assert engine.begin_count == 1
    assert engine.statements == ["delete", "update"]


def _services(engine: object) -> ApiServices:
    return ApiServices(
        engine=cast(Engine, engine),
        job_backend=cast(Any, object()),
        secret_store=cast(Any, object()),
        result_store=cast(Any, object()),
        jwt_secret="jwt-test-secret",
    )


class _ReadOnlyEngine:
    def __init__(self, rows: list[list[dict[str, Any]]]) -> None:
        self.rows = rows
        self.statements: list[str] = []
        self.connect_count = 0
        self.begin_count = 0

    def connect(self) -> _Connection:
        self.connect_count += 1
        return _Connection(self)

    def begin(self) -> _Connection:
        self.begin_count += 1
        raise AssertionError("is_token_revoked must not open a write transaction")


class _WriteEngine:
    def __init__(self) -> None:
        self.rows: list[list[dict[str, Any]]] = []
        self.statements: list[str] = []
        self.begin_count = 0

    def begin(self) -> _Connection:
        self.begin_count += 1
        return _Connection(self)


class _Connection:
    def __init__(self, engine: _ReadOnlyEngine | _WriteEngine) -> None:
        self.engine = engine

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def execute(self, statement: object) -> _Result:
        visit_name = getattr(statement, "__visit_name__", type(statement).__name__)
        self.engine.statements.append(str(visit_name))
        if self.engine.rows:
            return _Result(self.engine.rows.pop(0))
        return _Result([])


class _Result:
    rowcount = 1

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None
