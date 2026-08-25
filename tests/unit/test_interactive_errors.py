"""会话缝错误分类器单测(设计 §5.4 码表 + §4.1"永不按消息文本")。

码表用例逐条覆盖设计表格里的每个码;另外三组用例守住三个真机形态:
dmPython 的 `args[0].code`、建连期的 `SystemError.__context__`、PyMySQL 的
`OperationalError(errno, msg)`。
"""

from __future__ import annotations

import pytest

from app.dbclients.interactive.errors import (
    DM_ERROR_CODES,
    MYSQL_ERROR_CODES,
    DMErrorClassifier,
    MySQLErrorClassifier,
    driver_error_code,
)
from app.dbclients.interactive.protocol import ClassifiedError, ErrorCategory, ErrorClassifier
from app.domain.datasource import DbType
from tests.unit._interactive_fakes import dm_connect_error, dm_error, mysql_error

# ── §5.4 码表逐条 ───────────────────────────────────────────────────────────

DM_TABLE = [
    (-2501, ErrorCategory.AUTH_FAILED, False),
    (-70028, ErrorCategory.HOST_UNREACHABLE, False),
    (-6515, ErrorCategory.CANCELLED, True),
    (-2104, ErrorCategory.STATEMENT_ERROR, False),
    (-2106, ErrorCategory.STATEMENT_ERROR, False),
]

MYSQL_TABLE = [
    (1045, ErrorCategory.AUTH_FAILED, False),
    (2003, ErrorCategory.HOST_UNREACHABLE, False),
    (2005, ErrorCategory.HOST_UNREACHABLE, False),
    (1317, ErrorCategory.CANCELLED, True),
    (3024, ErrorCategory.TIMEOUT, True),
    (1054, ErrorCategory.STATEMENT_ERROR, False),
    (1064, ErrorCategory.STATEMENT_ERROR, False),
    (1146, ErrorCategory.STATEMENT_ERROR, False),
    (2006, ErrorCategory.CONNECTION_DEAD, False),
    (2013, ErrorCategory.CONNECTION_DEAD, False),
]


@pytest.mark.parametrize(("code", "category", "confirmed"), DM_TABLE)
def test_dm_code_table(code: int, category: ErrorCategory, confirmed: bool) -> None:
    classified = DMErrorClassifier().classify(dm_error(code))
    assert classified.category is category
    assert classified.driver_code == code
    assert classified.server_confirmed is confirmed


@pytest.mark.parametrize(("code", "category", "confirmed"), MYSQL_TABLE)
def test_mysql_code_table(code: int, category: ErrorCategory, confirmed: bool) -> None:
    classified = MySQLErrorClassifier().classify(mysql_error(code))
    assert classified.category is category
    assert classified.driver_code == code
    assert classified.server_confirmed is confirmed


def test_code_tables_cover_exactly_the_documented_codes() -> None:
    """码表是契约的一部分:改动必须同步改这里,防止悄悄增删。"""
    assert dict(DM_ERROR_CODES) == {code: category for code, category, _ in DM_TABLE}
    assert dict(MYSQL_ERROR_CODES) == {code: category for code, category, _ in MYSQL_TABLE}


def test_classifiers_declare_their_dialect() -> None:
    assert DMErrorClassifier().db_type is DbType.DM
    assert MySQLErrorClassifier().db_type is DbType.MYSQL
    assert isinstance(DMErrorClassifier(), ErrorClassifier)
    assert isinstance(MySQLErrorClassifier(), ErrorClassifier)


# ── 真机异常形态 ────────────────────────────────────────────────────────────


def test_dm_code_lives_on_the_dmerror_object_not_in_int_args() -> None:
    """dmPython 的码在 `args[0].code`;按"第一个 int 参数"取码对 DM 恒失败。"""
    exc = dm_error(-6515)
    assert not any(isinstance(arg, int) for arg in exc.args)
    assert driver_error_code(exc) == -6515


def test_dm_connect_failure_code_is_recovered_from_context() -> None:
    """建连失败抛的是 SystemError,真异常挂 `__context__` —— 不回溯就丢码。"""
    exc = dm_connect_error(-2501)
    assert driver_error_code(exc) == -2501
    assert DMErrorClassifier().classify(exc).category is ErrorCategory.AUTH_FAILED


def test_mysql_code_comes_from_errno_style_args() -> None:
    assert driver_error_code(mysql_error(1317)) == 1317


def test_cause_chain_is_bounded_and_cycle_safe() -> None:
    first = RuntimeError("a")
    second = RuntimeError("b")
    first.__context__ = second
    second.__context__ = first  # 人为造环
    assert driver_error_code(first) is None


# ── "永不按消息文本" ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        "The operation is canceled",
        "操作被取消",
        # 本机 Windows 上 dmPython 的中文消息就是这样以 GBK 字节回来的乱码。
        "������ȡ��",
        "",
    ],
)
def test_classification_ignores_message_language(message: str) -> None:
    classified = DMErrorClassifier().classify(dm_error(-6515, message))
    assert classified.category is ErrorCategory.CANCELLED
    assert classified.server_confirmed is True


def test_cancel_looking_message_with_unknown_code_stays_unknown() -> None:
    """消息写着 canceled 也不算数 —— 只有码说了算。"""
    classified = DMErrorClassifier().classify(dm_error(-99999, "the operation is canceled"))
    assert classified.category is ErrorCategory.UNKNOWN
    assert classified.server_confirmed is False


def test_unknown_code_never_claims_server_confirmed() -> None:
    classified = MySQLErrorClassifier().classify(mysql_error(4242))
    assert classified.category is ErrorCategory.UNKNOWN
    assert classified.server_confirmed is False
    assert classified.connection_aborted is False


@pytest.mark.parametrize("exc", [ConnectionResetError(), BrokenPipeError(), EOFError()])
def test_socket_exceptions_are_connection_dead(exc: BaseException) -> None:
    classified = DMErrorClassifier().classify(exc)
    assert classified.category is ErrorCategory.CONNECTION_DEAD
    assert classified.connection_aborted is True


# ── 摘要脱敏与 error_code 形态 ──────────────────────────────────────────────


def test_summary_redacts_password_and_urls() -> None:
    exc = mysql_error(1045, "Access denied for password='hunter2' at mysql://host:3306/db")
    summary = MySQLErrorClassifier().classify(exc).summary
    assert "hunter2" not in summary
    assert "***REDACTED***" in summary
    assert "<redacted-url>" in summary
    assert "code=1045" in summary


def test_bad_column_summary_carries_the_driver_text_for_the_user() -> None:
    """改表后按旧元数据展开 `*` 的真实形态:摘要必须带得走列名,否则用户无从判断。"""
    classified = MySQLErrorClassifier().classify(
        mysql_error(1054, "Unknown column 'legacy_col' in 'field list'")
    )
    assert classified.category is ErrorCategory.STATEMENT_ERROR
    assert classified.error_code == "statement_error:1054"
    assert "Unknown column 'legacy_col'" in classified.summary


def test_summary_is_truncated() -> None:
    summary = MySQLErrorClassifier().classify(mysql_error(1064, "x" * 5000)).summary
    assert len(summary) < 400
    assert summary.endswith("...")


def test_error_code_string_shape() -> None:
    assert ClassifiedError(ErrorCategory.CANCELLED, -6515).error_code == "cancelled:-6515"
    assert ClassifiedError(ErrorCategory.LOGIN_TIMEOUT).error_code == "login_timeout"
    # console_statements.error_code 是 String(64)。
    assert len(ClassifiedError(ErrorCategory.HOST_UNREACHABLE, -70028).error_code) <= 64
