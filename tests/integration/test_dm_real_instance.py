"""DM 真实例集成验证(backlog「DM Certified 宣称前必做」条目的 hard evidence)。

针对一台真实达梦 DM8 实例,逐项验证 backlog 要求的五个维度:
连接 / SELECT 流式 / introspection / EXPLAIN / 软取消,外加 ColumnType
映射与 DBMS_METADATA DDL。

GH Actions 无可信 DM 镜像(见 backlog),本文件**不在 CI 跑**:
缺 `DATAOPS_TEST_DM_HOST` 等环境变量时整文件 skip。运行方式(持牌环境):

    export DATAOPS_TEST_DM_HOST=127.0.0.1 DATAOPS_TEST_DM_PORT=5236 \
           DATAOPS_TEST_DM_USERNAME=SYSDBA \
           DATAOPS_TEST_DM_PASSWORD=...(从本机密钥文件读,勿写进 shell history)
    uv run pytest tests/integration/test_dm_real_instance.py -v

证据归档:运行 stdout 进 PR / docs(见 docs/acceptance-dm-certified.md)。
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterator
from typing import Any

import pytest

from app.dbclients.dm_adapter import DMAdapter, QueryCancelledError
from app.dbclients.dm_compare import HEX_TO_UINT64_TO_NUMBER, hex_to_uint64_expression
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.schema import Column, ColumnType
from app.domain.secret import HashedRef, RotationReport, SecretKind, SecretRef

_HOST = os.environ.get("DATAOPS_TEST_DM_HOST")
_PORT = int(os.environ.get("DATAOPS_TEST_DM_PORT", "5236"))
_USER = os.environ.get("DATAOPS_TEST_DM_USERNAME", "SYSDBA")
_PASSWORD = os.environ.get("DATAOPS_TEST_DM_PASSWORD")
_SCHEMA = os.environ.get("DATAOPS_TEST_DM_DATABASE", "SYSDBA")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (_HOST and _PASSWORD),
        reason="real DM instance required (DATAOPS_TEST_DM_HOST / _PASSWORD unset)",
    ),
]

_TABLE = "DOSV2_DM_VERIFY"
_SEED_ROWS = 2500  # > fetch_chunk_size(1000),强制流式跨多个 fetchmany 批次


class _EnvSecretStore:
    """测试桩:reveal 即返回环境变量里的 DM 密码(只在持牌环境进程内存在)。"""

    def __init__(self, password: str | None = None) -> None:
        self._password = password if password is not None else (_PASSWORD or "")

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


def _conn_info() -> DatasourceConnInfo:
    return DatasourceConnInfo(
        host=_HOST or "",
        port=_PORT,
        username=_USER,
        database=_SCHEMA,
        password_ref=SecretRef(ref="env://dm-test", kind=SecretKind.DATASOURCE_PASSWORD),
        db_type=DbType.DM,
    )


def _raw_dm_connection() -> Any:
    # seed/清理需要直连;与 dm_adapter 同款 importlib 动态加载(dmPython 无类型存根)
    dm = importlib.import_module("dmPython")
    return dm.connect(user=_USER, password=_PASSWORD, server=_HOST, port=_PORT)


@pytest.fixture(scope="module")
def seeded_table() -> Iterator[str]:
    conn = _raw_dm_connection()
    cur = conn.cursor()
    try:
        try:
            cur.execute(f"DROP TABLE {_TABLE}")
        except Exception:
            pass
        cur.execute(
            f"CREATE TABLE {_TABLE} ("
            "ID INT PRIMARY KEY, LABEL VARCHAR(64), AMOUNT DECIMAL(10,2), CREATED_AT TIMESTAMP)"
        )
        cur.executemany(
            f"INSERT INTO {_TABLE} VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            [(i, f"row-{i}", round(i * 1.5, 2)) for i in range(1, _SEED_ROWS + 1)],
        )
        conn.commit()
        yield _TABLE
    finally:
        try:
            cur.execute(f"DROP TABLE {_TABLE}")
            conn.commit()
        except Exception:
            pass
        cur.close()
        conn.close()


@pytest.fixture()
def adapter() -> DMAdapter:
    return DMAdapter(_conn_info(), _EnvSecretStore())


# ── 1. 连接 ───────────────────────────────────────────────────────────────────


def test_connection_and_server_version(adapter: DMAdapter) -> None:
    assert adapter.test_connection() is True
    assert adapter.last_server_version is not None
    assert adapter.last_server_version.startswith("DM")
    print(f"\n[evidence] server_version = {adapter.last_server_version}")


def test_connection_wrong_password_classified() -> None:
    bad = DMAdapter(_conn_info(), _EnvSecretStore(password="definitely-wrong-pw-1"))
    assert bad.test_connection() is False
    assert bad.last_connection_error is not None
    print(f"\n[evidence] wrong-pw error_summary = {bad.last_connection_error}")


# ── 2. SELECT 流式(跨批次)──────────────────────────────────────────────────


def test_streaming_select_full_scan(adapter: DMAdapter, seeded_table: str) -> None:
    captured: list[list[Column]] = []
    streaming = DMAdapter(_conn_info(), _EnvSecretStore(), column_sink=captured.append)
    rows = list(
        streaming.execute_select(
            f"SELECT ID, LABEL, AMOUNT, CREATED_AT FROM {seeded_table} ORDER BY ID", {}
        )
    )
    assert len(rows) == _SEED_ROWS
    assert rows[0].values[0] == 1
    assert rows[-1].values[0] == _SEED_ROWS
    assert captured, "column_sink 未被调用"
    types = {c.name.upper(): c.type for c in captured[0]}
    print(f"\n[evidence] streamed {len(rows)} rows; cursor column types = {types}")
    assert types["ID"] is ColumnType.INTEGER
    assert types["LABEL"] is ColumnType.STRING
    assert types["AMOUNT"] is ColumnType.DECIMAL
    assert types["CREATED_AT"] is ColumnType.DATETIME


# ── 3. 软取消 ─────────────────────────────────────────────────────────────────


def test_soft_cancel_mid_stream(seeded_table: str) -> None:
    state = {"cancel": False}
    cancelling = DMAdapter(
        _conn_info(),
        _EnvSecretStore(),
        cancel_check=lambda: state["cancel"],
        fetch_chunk_size=200,
    )
    consumed = 0
    with pytest.raises(QueryCancelledError):
        for _row in cancelling.execute_select(f"SELECT ID FROM {seeded_table} ORDER BY ID", {}):
            consumed += 1
            if consumed == 200:  # 第一批消费完后请求取消 → 下一批边界应抛
                state["cancel"] = True
    assert 0 < consumed < _SEED_ROWS
    print(f"\n[evidence] soft-cancel fired after {consumed} rows (of {_SEED_ROWS})")


def test_kill_query_honestly_false(adapter: DMAdapter) -> None:
    assert adapter.capabilities.server_side_cancel is False
    assert adapter.kill_query("any") is False


# ── 4. introspection ─────────────────────────────────────────────────────────


def test_introspection_chain(adapter: DMAdapter, seeded_table: str) -> None:
    schemas = [s.name for s in adapter.list_schemas()]
    assert _SCHEMA in schemas
    tables = [t.name for t in adapter.list_tables(_SCHEMA)]
    assert seeded_table in tables
    columns = adapter.list_columns(_SCHEMA, seeded_table)
    by_name = {c.name.upper(): c for c in columns}
    assert by_name["ID"].primary_key is True
    assert by_name["ID"].type is ColumnType.INTEGER
    assert by_name["LABEL"].type is ColumnType.STRING
    assert by_name["AMOUNT"].type is ColumnType.DECIMAL
    assert by_name["CREATED_AT"].type is ColumnType.DATETIME
    assert by_name["LABEL"].driver_type, "driver_type 应保留原始类型串"
    indexes = adapter.list_indexes(_SCHEMA, seeded_table)
    assert any(i.is_primary for i in indexes)
    print(
        f"\n[evidence] schemas={len(schemas)} tables={len(tables)} "
        f"columns={[(c.name, c.type.value, c.driver_type) for c in columns]} "
        f"indexes={[(i.name, i.is_primary, i.is_unique) for i in indexes]}"
    )


def test_table_ddl(adapter: DMAdapter, seeded_table: str) -> None:
    ddl = adapter.get_table_ddl(_SCHEMA, seeded_table)
    assert seeded_table in ddl.upper()
    print(f"\n[evidence] DDL head = {ddl[:120]!r}")


# ── 5. EXPLAIN ────────────────────────────────────────────────────────────────


def test_explain(adapter: DMAdapter, seeded_table: str) -> None:
    plan = adapter.explain(f"SELECT * FROM {seeded_table} WHERE ID = 1")
    assert plan.operation == "EXPLAIN"
    rows = plan.details.get("rows")
    assert rows, "EXPLAIN 应返回计划行"
    print(f"\n[evidence] explain rows = {len(rows)}; first = {str(rows[0])[:120]!r}")


# ── 6. 下推哈希 hex -> uint64 选型(PR「达梦哈希拆半字」的 hard evidence)────────


def test_dm_hex_to_uint64_full_width_x_model_is_signed(adapter: DMAdapter) -> None:
    """记录达梦的真实语义:X 格式模型按两补码 signed 64 位解释。

    这正是不能直接用 TO_NUMBER(h, 'X'*16) 的原因 —— 满宽会让 MD5 首位 >= 8 的行
    (约一半)哈希变负,与 compare_row_hash64(无符号)全面对不上,静默产假差异。
    本用例若某天在新版达梦上变红,说明该版本改了语义,可以考虑简化表达式。
    """
    rows = adapter._query_tuples(
        "SELECT TO_NUMBER('00000000DEADBEEF', 'XXXXXXXXXXXXXXXX'),"
        " TO_NUMBER('FFFFFFFFFFFFFFFF', 'XXXXXXXXXXXXXXXX') FROM DUAL"
    )
    low, high = int(rows[0][0]), int(rows[0][1])
    assert low == 0xDEADBEEF
    assert high == -1, "达梦满宽 X 模型返回 -1 而非 18446744073709551615"
    print(f"\n[evidence] full-width X model: DEADBEEF={low} FFFF...={high} (signed64)")


def test_dm_hex_to_uint64_split_halves_matches_unsigned(adapter: DMAdapter) -> None:
    """拆两个 32 位半字后与无符号语义逐位一致(含全部符号边界)。"""
    samples = [
        "0000000000000000",
        "00000000DEADBEEF",
        "7FFFFFFFFFFFFFFF",
        "8000000000000000",
        "FEDCBA9876543210",
        "FFFFFFFFFFFFFFFF",
    ]
    selects = ", ".join(
        hex_to_uint64_expression(f"'{sample}'", HEX_TO_UINT64_TO_NUMBER) for sample in samples
    )
    row = adapter._query_tuples(f"SELECT {selects} FROM DUAL")[0]

    got = [int(value) for value in row]
    expected = [int(sample, 16) for sample in samples]
    assert got == expected
    print(f"\n[evidence] split-halves on real DM: {list(zip(samples, got, strict=True))}")


def test_dm_probe_selects_to_number_on_real_instance(adapter: DMAdapter) -> None:
    """真实例上探测应选中快路。"""
    mode = adapter._probe_hex_mode()

    assert mode == HEX_TO_UINT64_TO_NUMBER
    print(f"\n[evidence] probed mode on real DM = {mode}")
