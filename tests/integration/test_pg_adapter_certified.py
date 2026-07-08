"""PostgreSQL adapter 真实例集成验证(backlog「PostgreSQL adapter Certified 前置项」
PG-1/PG-2 的 hard evidence)。

针对 CI 的真实 PostgreSQL 16 service(pg-integration job)逐项验证 backlog #98
评论要求的维度,外加常见类型全景映射:

  1. statement_timeout **真触发**(psycopg3 set_config 路径,合并前修过静默失效 bug):
     设 1s 超时跑 pg_sleep(3),断言结构化超时错误(SQLSTATE 57014)且在 sleep 前中断。
  2. jsonb / json 列:建表插值,断言 ColumnType 映射为 JSON 且值反序列化为 Python 对象。
  3. information_schema 注入面:带引号 / 分号 / 注释符的表名正确转义round-trip;
     恶意 schema / table 入参被标识符 guard 拒(defense-in-depth),无注入。
  4. cancel_safe 路径(#144):长查询发 cancel,断言取消生效、连接状态干净(服务端无残留
     在飞查询、后续查询正常)。
  5. 常见类型全景:numeric / timestamptz / bytea / int[] 各一列的 ColumnType 映射断言
     (流式 cursor 描述 + information_schema introspection 两路)。

Gating:缺 `DATAOPS_TEST_PG_URL`(或回退 `DATAOPS_DATABASE_URL`)整文件 skip。
CI 的 `PG Queue Concurrency` job 以 `pytest -m integration tests/integration/` 收集执行,
并注入 `DATAOPS_TEST_PG_URL` 指向 postgres:16 service —— 故本文件在该 job 自动真跑。

本地(无 PG)跑法:
    export DATAOPS_TEST_PG_URL=postgresql+psycopg://user:pw@127.0.0.1:5432/db
    uv run pytest tests/integration/test_pg_adapter_certified.py -v

证据归档:运行 stdout 进 PR / docs(见 docs/acceptance-pg-certified.md)。
"""

from __future__ import annotations

import importlib
import os
import time
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.engine import make_url

from app.dbclients.postgresql_adapter import PostgresqlAdapter, QueryCancelledError
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.schema import Column, ColumnType
from app.domain.secret import HashedRef, RotationReport, SecretKind, SecretRef

_RAW_URL = os.environ.get("DATAOPS_TEST_PG_URL") or os.environ.get("DATAOPS_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _RAW_URL,
        reason="real PG instance required (DATAOPS_TEST_PG_URL / DATAOPS_DATABASE_URL unset)",
    ),
]

_SCHEMA = "public"
_TYPES_TABLE = "pg_cert_types"
_JSON_TABLE = "pg_cert_json"
# 表名内嵌双引号 / 分号 / 注释符 —— 只有正确转义 + 参数化 introspection 才不炸。
_WEIRD_TABLE = 'pg_cert_evil";--'


class _EnvSecretStore:
    """测试桩:reveal 即返回从 DATAOPS_TEST_PG_URL 解析出的密码(仅进程内存)。"""

    def __init__(self, password: str) -> None:
        self._password = password

    def reveal_secret(self, ref: SecretRef) -> str:
        assert ref.kind is SecretKind.DATASOURCE_PASSWORD
        return self._password

    def store_secret(self, plaintext: str, kind: SecretKind) -> SecretRef:
        raise NotImplementedError

    def rotate_secret(self, ref: SecretRef, new_plaintext: str) -> SecretRef:
        raise NotImplementedError

    def delete_secret(self, ref: SecretRef) -> None:
        raise NotImplementedError

    def hash_password(self, plaintext: str) -> HashedRef:
        raise NotImplementedError

    def verify_password(self, plaintext: str, ref: HashedRef) -> bool:
        raise NotImplementedError

    def rotate_master_key(self, new_key: bytes) -> RotationReport:
        raise NotImplementedError


def _pg_parts() -> dict[str, Any]:
    url = make_url(_RAW_URL or "")
    return {
        "host": url.host or "127.0.0.1",
        "port": int(url.port or 5432),
        "user": url.username or "postgres",
        "password": url.password or "",
        "dbname": url.database or "postgres",
    }


def _conn_info() -> DatasourceConnInfo:
    parts = _pg_parts()
    return DatasourceConnInfo(
        host=parts["host"],
        port=parts["port"],
        username=parts["user"],
        database=parts["dbname"],
        password_ref=SecretRef(ref="env://pg-test", kind=SecretKind.DATASOURCE_PASSWORD),
        db_type=DbType.POSTGRESQL,
    )


def _secret_store(password: str | None = None) -> _EnvSecretStore:
    return _EnvSecretStore(password if password is not None else _pg_parts()["password"])


def _make_adapter(**overrides: Any) -> PostgresqlAdapter:
    secret_store = overrides.pop("secret_store", None) or _secret_store()
    return PostgresqlAdapter(_conn_info(), secret_store, **overrides)


def _raw_connection() -> Any:
    # seed / 清理 / pg_stat_activity 走直连;psycopg 动态加载(与 adapter 同款)。
    psycopg = importlib.import_module("psycopg")
    parts = _pg_parts()
    return psycopg.connect(
        host=parts["host"],
        port=parts["port"],
        user=parts["user"],
        password=parts["password"],
        dbname=parts["dbname"],
        autocommit=True,
    )


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@pytest.fixture(scope="module")
def seeded() -> Iterator[dict[str, str]]:
    conn = _raw_connection()
    cur = conn.cursor()
    weird_q = _quote_ident(_WEIRD_TABLE)
    try:
        for stmt in (
            f"DROP TABLE IF EXISTS {_TYPES_TABLE}",
            f"DROP TABLE IF EXISTS {_JSON_TABLE}",
            f"DROP TABLE IF EXISTS {weird_q}",
        ):
            cur.execute(stmt)
        cur.execute(
            f"CREATE TABLE {_TYPES_TABLE} ("
            "id integer PRIMARY KEY, num numeric(10,2), ts timestamptz, "
            "raw bytea, arr integer[])"
        )
        cur.execute(
            f"INSERT INTO {_TYPES_TABLE} (id, num, ts, raw, arr) VALUES "
            "(1, 123.45, TIMESTAMPTZ '2024-01-02 03:04:05+00', "
            "'\\x0102ff'::bytea, ARRAY[10, 20, 30])"
        )
        cur.execute(f"CREATE TABLE {_JSON_TABLE} (id integer PRIMARY KEY, jb jsonb, js json)")
        cur.execute(
            f"INSERT INTO {_JSON_TABLE} (id, jb, js) VALUES "
            """(1, '{"k": "v", "n": 1}'::jsonb, '[1, 2, 3]'::json)"""
        )
        cur.execute(f"CREATE TABLE {weird_q} (id integer PRIMARY KEY)")
        yield {"types": _TYPES_TABLE, "json": _JSON_TABLE, "weird": _WEIRD_TABLE}
    finally:
        for stmt in (
            f"DROP TABLE IF EXISTS {_TYPES_TABLE}",
            f"DROP TABLE IF EXISTS {_JSON_TABLE}",
            f"DROP TABLE IF EXISTS {weird_q}",
        ):
            try:
                cur.execute(stmt)
            except Exception:
                pass
        cur.close()
        conn.close()


# ── 0. 连接 smoke ─────────────────────────────────────────────────────────────


def test_connection_and_server_version() -> None:
    adapter = _make_adapter()
    assert adapter.test_connection() is True
    assert adapter.last_server_version is not None
    print(f"\n[evidence] server_version = {adapter.last_server_version}")


def test_wrong_password_classified_not_leaked() -> None:
    bad = _make_adapter(secret_store=_secret_store("definitely-wrong-pw-xyz"))
    assert bad.test_connection() is False
    assert bad.last_connection_error is not None
    assert "definitely-wrong-pw-xyz" not in bad.last_connection_error
    print(f"\n[evidence] wrong-pw error_summary = {bad.last_connection_error}")


# ── 1. statement_timeout 真触发(set_config 路径)────────────────────────────


def test_statement_timeout_fires_structured_error() -> None:
    psycopg_errors = importlib.import_module("psycopg.errors")
    adapter = _make_adapter(statement_timeout_seconds=1, cursor_max_hold_seconds=0)
    start = time.monotonic()
    with pytest.raises(psycopg_errors.QueryCanceled) as exc_info:
        list(adapter.execute_select("SELECT pg_sleep(3)", {}))
    elapsed = time.monotonic() - start
    # SQLSTATE 57014 = query_canceled(statement_timeout 命中)。
    assert getattr(exc_info.value, "sqlstate", None) == "57014"
    # 若 set_config 静默失效,pg_sleep(3) 会跑满 3s 才返回 —— 断言在此之前中断。
    assert elapsed < 3.0
    print(f"\n[evidence] statement_timeout fired: sqlstate=57014 after {elapsed:.2f}s (< 3s sleep)")


# ── 2. jsonb / json 列映射 + 值序列化 ─────────────────────────────────────────


def test_jsonb_json_column_type_and_value_serialization(seeded: dict[str, str]) -> None:
    captured: list[list[Column]] = []
    adapter = _make_adapter(column_sink=captured.append)
    rows = list(adapter.execute_select(f"SELECT jb, js FROM {seeded['json']} WHERE id = 1", {}))
    assert captured, "column_sink 未被调用"
    types = {c.name: c.type for c in captured[0]}
    assert types["jb"] is ColumnType.JSON
    assert types["js"] is ColumnType.JSON
    # psycopg3 默认把 jsonb/json 反序列化成 Python 对象。
    jb_value, js_value = rows[0].values
    assert jb_value == {"k": "v", "n": 1}
    assert js_value == [1, 2, 3]

    # introspection 路径同样落 JSON。
    cols = {c.name: c for c in adapter.list_columns(_SCHEMA, seeded["json"])}
    assert cols["jb"].type is ColumnType.JSON
    assert cols["js"].type is ColumnType.JSON
    print(
        f"\n[evidence] jsonb/json cursor types={types} "
        f"jb_value={jb_value!r} js_value={js_value!r} "
        f"introspection jb.driver_type={cols['jb'].driver_type}"
    )


# ── 3. information_schema 注入面 ──────────────────────────────────────────────


def test_introspection_special_identifier_escaped_and_no_injection(
    seeded: dict[str, str],
) -> None:
    adapter = _make_adapter()
    # 带引号/分号/注释符的真实表名,经参数化 introspection 原样 round-trip,无报错。
    table_names = [t.name for t in adapter.list_tables(_SCHEMA)]
    assert seeded["weird"] in table_names, "特殊字符表名未被正确转义 / 检索"

    # 恶意 schema / table 入参:标识符 guard 拒(defense-in-depth,绝不拼进 SQL)。
    with pytest.raises(ValueError):
        adapter.list_tables('public"; DROP TABLE pg_cert_types; --')
    with pytest.raises(ValueError):
        adapter.list_columns(_SCHEMA, 'x"; DROP TABLE pg_cert_types; --')
    with pytest.raises(ValueError):
        adapter.list_indexes(_SCHEMA, "t'/* injected */")

    # 注入尝试后原表仍在(证明没有任何 DROP 被执行)。
    assert seeded["types"] in [t.name for t in adapter.list_tables(_SCHEMA)]
    print(f"\n[evidence] weird table listed verbatim={seeded['weird']!r}; injection args rejected")


# ── 4. cancel_safe 真取消 + 连接干净(#144)──────────────────────────────────


def test_server_side_cancel_is_clean(seeded: dict[str, str]) -> None:
    start = time.monotonic()

    def cancel_check() -> bool:
        return time.monotonic() - start > 0.5

    adapter = _make_adapter(
        cancel_check=cancel_check,
        statement_timeout_seconds=0,  # 隔离:只让 cancel 触发,不掺 timeout
        cursor_max_hold_seconds=0,
        cancel_poll_interval_seconds=0.1,
    )
    t0 = time.monotonic()
    with pytest.raises(QueryCancelledError):
        list(adapter.execute_select("SELECT pg_sleep(30)", {}))
    elapsed = time.monotonic() - t0
    # cancel_safe 真下发取消,execute() 早于 30s sleep 中断。
    assert elapsed < 15.0

    # 连接状态干净其一:服务端无残留在飞 pg_sleep(cancel 真作用于 backend)。
    conn = _raw_connection()
    cur = conn.cursor()
    try:
        lingering = -1
        for _ in range(20):
            cur.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE state = 'active' AND query LIKE '%pg_sleep(30)%' "
                "AND pid <> pg_backend_pid()"
            )
            row = cur.fetchone()
            lingering = int(row[0]) if row else -1
            if lingering == 0:
                break
            time.sleep(0.25)
        assert lingering == 0, "服务端仍有残留 pg_sleep 查询,取消未真作用于 backend"
    finally:
        cur.close()
        conn.close()

    # 连接状态干净其二:后续查询正常返回(adapter 未卡在坏连接状态)。
    fresh = _make_adapter()
    assert [r.values for r in fresh.execute_select("SELECT 1 AS ok", {})] == [[1]]
    print(f"\n[evidence] cancel fired after {elapsed:.2f}s; no lingering backend; fresh SELECT ok")


# ── 5. 常见类型全景映射 ───────────────────────────────────────────────────────


def test_common_type_panorama_mapping(seeded: dict[str, str]) -> None:
    captured: list[list[Column]] = []
    adapter = _make_adapter(column_sink=captured.append)
    rows = list(
        adapter.execute_select(f"SELECT num, ts, raw, arr FROM {seeded['types']} WHERE id = 1", {})
    )
    assert captured, "column_sink 未被调用"
    stream_types = {c.name: c.type for c in captured[0]}
    assert stream_types["num"] is ColumnType.DECIMAL
    assert stream_types["ts"] is ColumnType.DATETIME
    assert stream_types["raw"] is ColumnType.BYTES
    # int[] 无对应统一枚举 → UNKNOWN(契约:不臆造,留 driver_type 备查)。
    assert stream_types["arr"] is ColumnType.UNKNOWN

    num_v, ts_v, raw_v, arr_v = rows[0].values
    assert isinstance(num_v, Decimal) and num_v == Decimal("123.45")
    assert isinstance(ts_v, datetime) and ts_v.tzinfo is not None
    assert bytes(raw_v) == b"\x01\x02\xff"
    assert list(arr_v) == [10, 20, 30]

    # introspection 路径:同样映射,且 array 的 driver_type(udt_name)保留供人工核对。
    cols = {c.name: c for c in adapter.list_columns(_SCHEMA, seeded["types"])}
    assert cols["num"].type is ColumnType.DECIMAL
    assert cols["ts"].type is ColumnType.DATETIME
    assert cols["raw"].type is ColumnType.BYTES
    assert cols["arr"].type is ColumnType.UNKNOWN
    assert cols["arr"].driver_type is not None
    assert cols["id"].primary_key is True
    print(
        f"\n[evidence] stream types={stream_types}; "
        f"values=({num_v!r}, {ts_v.isoformat()}, {bytes(raw_v)!r}, {list(arr_v)}); "
        f"introspection arr.driver_type={cols['arr'].driver_type}"
    )
