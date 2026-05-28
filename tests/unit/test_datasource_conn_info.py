"""DatasourceConnInfo + DbType 单元测试(契约 §3.2 配套冻结模型)。"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.secret import SecretKind, SecretRef


def _good_password_ref() -> SecretRef:
    return SecretRef(ref="sr_dummy_abc123", kind=SecretKind.DATASOURCE_PASSWORD)


def _good_conn_info(**overrides: Any) -> DatasourceConnInfo:
    base: dict[str, Any] = dict(
        host="db.example.com",
        port=3306,
        username="alice",
        database="appdb",
        password_ref=_good_password_ref(),
        db_type=DbType.MYSQL,
    )
    base.update(overrides)
    return DatasourceConnInfo(**base)


# ─── DbType ───


def test_dbtype_has_5_values() -> None:
    expected = {"mysql", "oracle", "dm", "db2", "postgresql"}
    assert {d.value for d in DbType} == expected


# ─── DatasourceConnInfo: 构造成功 ───


def test_conn_info_construct_minimal() -> None:
    ci = _good_conn_info()
    assert ci.host == "db.example.com"
    assert ci.port == 3306
    assert ci.database == "appdb"
    assert ci.password_ref.kind is SecretKind.DATASOURCE_PASSWORD
    assert ci.db_type is DbType.MYSQL
    assert ci.extra == {}


def test_conn_info_construct_with_extra() -> None:
    ci = _good_conn_info(
        extra={"service_name": "ORCL", "ssl_mode": "require", "connect_timeout": 5}
    )
    assert ci.extra["service_name"] == "ORCL"


# ─── frozen=True 不可变 ───


def test_conn_info_is_frozen() -> None:
    ci = _good_conn_info()
    with pytest.raises(ValidationError):
        ci.host = "evil.example.com"


# ─── password_ref.kind 校验(与 T3 R4 一脉相承) ───


@pytest.mark.parametrize(
    "wrong_kind",
    [
        SecretKind.AI_API_KEY,
        SecretKind.MFA_TOTP_SEED,
        SecretKind.OAUTH_TOKEN,
        SecretKind.WEBHOOK_SECRET,
        SecretKind.SIGNED_URL_SECRET,
    ],
)
def test_conn_info_rejects_non_datasource_password_kind(wrong_kind: SecretKind) -> None:
    bad_ref = SecretRef(ref="sr_xx", kind=wrong_kind)
    with pytest.raises(ValidationError) as exc_info:
        _good_conn_info(password_ref=bad_ref)
    assert "DATASOURCE_PASSWORD" in str(exc_info.value)


# ─── 字段验证 ───


def test_conn_info_rejects_empty_host() -> None:
    with pytest.raises(ValidationError):
        _good_conn_info(host="")


def test_conn_info_rejects_empty_database() -> None:
    with pytest.raises(ValidationError):
        _good_conn_info(database="")


def test_conn_info_rejects_empty_username() -> None:
    with pytest.raises(ValidationError):
        _good_conn_info(username="")


@pytest.mark.parametrize("bad_port", [0, -1, 65536, 100000])
def test_conn_info_rejects_invalid_port(bad_port: int) -> None:
    with pytest.raises(ValidationError):
        _good_conn_info(port=bad_port)


# ─── 5 个 DbType 都能构造 ───


@pytest.mark.parametrize("dbt", list(DbType))
def test_conn_info_accepts_all_dbtypes(dbt: DbType) -> None:
    ci = _good_conn_info(db_type=dbt)
    assert ci.db_type is dbt
