"""数据源能力下发 + extra 校验单测。

期望值:mysql / dm / postgresql / db2 照抄收敛前前端的硬编码白名单(SqlWorkspaceView /
sqlIntelligence / useConsoleSessions),证明改读后端能力后这几类可用范围不变;
oracle / oceanbase_oracle 是 PR-6 以 Preview 档新放开的执行与 SQL 工具。
"""

from __future__ import annotations

import pytest

from app.api.datasource_capabilities import PREVIEW_EXECUTION_ALLOWED, datasource_capabilities
from app.domain.datasource import DbType, OperationPolicy
from app.domain.datasource_extra import DatasourceExtraError, validate_datasource_extra

_EXECUTION = {"mysql", "dm", "postgresql", "db2", "oracle", "oceanbase_oracle"}
_TOOLS = {"mysql", "dm", "postgresql", "oracle", "oceanbase_oracle"}
_SESSION = {"mysql", "dm"}
# 缺省策略下 explain 按钮:Oracle 另需 allow_oracle_plan_table(缺省关)
_EXPLAIN_BY_DEFAULT = {"mysql", "dm", "postgresql", "oceanbase_oracle"}


@pytest.mark.parametrize("db_type", list(DbType))
def test_capabilities_per_type(db_type: DbType) -> None:
    caps = datasource_capabilities(db_type, OperationPolicy())

    assert caps.execute is (db_type.value in _EXECUTION)
    assert caps.sql_tools is (db_type.value in _TOOLS)
    assert caps.metadata is (db_type.value in _TOOLS)
    assert caps.session_console is (db_type.value in _SESSION)
    assert caps.explain is (db_type.value in _EXPLAIN_BY_DEFAULT)


def test_maturity_and_preview_execution_gate() -> None:
    assert datasource_capabilities(DbType.MYSQL, OperationPolicy()).maturity == "certified"
    assert datasource_capabilities(DbType.DM, OperationPolicy()).maturity == "certified"
    assert datasource_capabilities(DbType.ORACLE, OperationPolicy()).maturity == "preview"
    # PR-6:Oracle / OB-Oracle 以 Preview 档放开执行
    assert DbType.ORACLE in PREVIEW_EXECUTION_ALLOWED
    assert DbType.OCEANBASE_ORACLE in PREVIEW_EXECUTION_ALLOWED


def test_oracle_explain_needs_plan_table_gate() -> None:
    gated = datasource_capabilities(DbType.ORACLE, OperationPolicy(allow_oracle_plan_table=True))
    assert gated.explain is True
    no_explain = OperationPolicy(allow_oracle_plan_table=True, allow_explain=False)
    assert datasource_capabilities(DbType.ORACLE, no_explain).explain is False


def test_only_allow_explain_narrows_capabilities() -> None:
    # execute 表达类型是否开放;SELECT 权限仍由执行入口按 operation_policy 校验
    no_select = datasource_capabilities(DbType.MYSQL, OperationPolicy(allow_select=False))
    assert no_select.execute is True
    assert no_select.sql_tools is True

    no_explain = datasource_capabilities(DbType.MYSQL, OperationPolicy(allow_explain=False))
    assert no_explain.execute is True
    assert no_explain.sql_tools is True
    assert no_explain.explain is False


def test_static_adapter_capabilities_are_passed_through() -> None:
    caps = datasource_capabilities(DbType.OCEANBASE_ORACLE, OperationPolicy())
    assert caps.ddl is True
    assert caps.compare_db_hash is False
    assert datasource_capabilities(DbType.MYSQL, OperationPolicy()).compare_db_hash is True


# ─────────────────────────── extra 校验 ───────────────────────────


@pytest.mark.parametrize(
    ("username", "extra"),
    [
        ("app_user", {"tenant": "t1", "cluster": "c1"}),
        ("app_user", {"tenant": "t1", "access_mode": "direct"}),
        ("app_user", {"tenant": "t1", "cluster": "c1", "charset": "utf8mb4"}),
        ("app_user@t1#c1", {}),  # 已拼好的完整登录名
    ],
)
def test_oceanbase_extra_accepts_valid_config(username: str, extra: dict[str, str]) -> None:
    validate_datasource_extra(DbType.OCEANBASE_ORACLE, username, extra)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({}, "tenant is required"),
        ({"tenant": "t1"}, "cluster is required"),
        ({"tenant": "t1", "cluster": "c1", "access_mode": "socks"}, "access_mode"),
        ({"tenant": "t1", "cluster": "c1", "dsn": "x"}, "unsupported OceanBase extra keys: dsn"),
    ],
)
def test_oceanbase_extra_rejects_invalid_config(extra: dict[str, str], message: str) -> None:
    with pytest.raises(DatasourceExtraError) as excinfo:
        validate_datasource_extra(DbType.OCEANBASE_ORACLE, "app_user", extra)
    assert message in str(excinfo.value)


@pytest.mark.parametrize("key", ["password", "db_passwd", "pwd", "api_token", "client_secret"])
def test_secret_like_extra_keys_are_rejected_for_every_type(key: str) -> None:
    for db_type in DbType:
        with pytest.raises(DatasourceExtraError) as excinfo:
            validate_datasource_extra(db_type, "app_user@t#c", {key: "x"})
        # 消息只含键名,不回显值
        assert "'x'" not in str(excinfo.value)


def test_existing_extra_keys_of_other_types_stay_accepted() -> None:
    validate_datasource_extra(DbType.ORACLE, "app_user", {"service_name": "ORCL", "dsn": "h:1/s"})
    validate_datasource_extra(DbType.MYSQL, "app_user", {"charset": "utf8mb4", "ssl": {"ca": "x"}})
