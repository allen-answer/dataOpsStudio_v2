"""达梦下推哈希的 hex -> uint64 选型单测。

核心要守的不是「快」,而是「换写法后数值一字不差」:该值必须等于
compare_row_hash64(md5[:8] 大端无符号),也等于 MySQL 侧 CONV(...,16,10)。
一旦不等,跨库对比与降级客户端哈希会静默产出海量假差异 —— 比慢严重得多。

真实例证据在 tests/integration/test_dm_real_instance.py::test_dm_hex_to_uint64_*。
"""

from __future__ import annotations

import hashlib
from typing import Any, cast

import pytest

from app.dbclients.dm_adapter import DMAdapter
from app.dbclients.dm_compare import (
    HEX_TO_UINT64_NESTED,
    HEX_TO_UINT64_TO_NUMBER,
    build_dm_compare_hash_plan,
    hex_to_uint64_expression,
)
from app.domain.compare import (
    CompareColumn,
    CompareHashLevel,
    CompareHashRequest,
    CompareRules,
    CompareTableRef,
    compare_row_hash64,
    normalized_compare_payload,
)
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.schema import ColumnType
from app.domain.secret import HashedRef, RotationReport, SecretKind, SecretRef
from app.infrastructure.secretstore.protocol import SecretStore

_HEXDIGITS = "0123456789ABCDEF"


def _eval_nested(hex_str: str) -> int:
    """复刻 nested 表达式语义:((0*16 + (INSTR(digits, SUBSTR(h,i,1)) - 1)) ...) x16。"""
    acc = 0
    for i in range(1, 17):
        acc = acc * 16 + _HEXDIGITS.find(hex_str[i - 1 : i])
    return acc


def _eval_halves(hex_str: str) -> int:
    """复刻 to_number 表达式语义:高 8 位 * 2^32 + 低 8 位。"""
    return int(hex_str[0:8], 16) * 4294967296 + int(hex_str[8:16], 16)


def _request(level: CompareHashLevel = CompareHashLevel.AGGREGATE) -> CompareHashRequest:
    return CompareHashRequest(
        table=CompareTableRef(schema_name="DW", name="T"),
        columns=[CompareColumn(name="AMT", type=ColumnType.DECIMAL)],
        key_columns=[CompareColumn(name="ID", type=ColumnType.INTEGER)],
        rules=CompareRules(),
        segment=None,
        level=level,
    )


@pytest.mark.parametrize(
    "hex_str",
    [
        "0000000000000000",
        "0000000000000001",
        "00000000DEADBEEF",
        "7FFFFFFFFFFFFFFF",
        "8000000000000000",  # signed 64 位翻负的分界
        "8000000000000001",
        "FEDCBA9876543210",
        "FFFFFFFFFFFFFFFE",
        "FFFFFFFFFFFFFFFF",  # 满宽 X 格式模型在达梦上返回 -1,拆半字必须绕开
    ],
)
def test_both_hex_modes_agree_on_boundary_values(hex_str: str) -> None:
    expected = int(hex_str, 16)

    assert _eval_nested(hex_str) == expected
    assert _eval_halves(hex_str) == expected


def test_hex_modes_match_compare_row_hash64_on_real_digests() -> None:
    """两种写法都必须等于客户端 / MySQL 侧算出来的那个 uint64。"""
    columns = [
        CompareColumn(name="ID", type=ColumnType.INTEGER),
        CompareColumn(name="NAME", type=ColumnType.STRING),
        CompareColumn(name="AMT", type=ColumnType.DECIMAL),
    ]
    rules = CompareRules()
    for seed in range(200):
        values: list[object] = [seed, f"name-{seed}", seed / 7]
        payload = normalized_compare_payload(columns, values, rules)
        digest_hex = hashlib.md5(payload.encode("utf-8"), usedforsecurity=False).hexdigest().upper()

        expected = compare_row_hash64(columns, values, rules)

        assert _eval_nested(digest_hex) == expected
        assert _eval_halves(digest_hex) == expected


def test_to_number_mode_never_uses_a_16_wide_format_model() -> None:
    """回归守卫:达梦的 X 格式模型按两补码 signed 64 位解释。

    TO_NUMBER('FFFFFFFFFFFFFFFF', 'X'*16) 在真实例上返回 -1 而非
    18446744073709551615(198/199 实测)。用满宽会让 MD5 首位 >= 8 的行
    (约一半)哈希变负,与 compare_row_hash64 全面对不上 —— 静默海量假差异。
    因此必须拆成两个 32 位半字,每半恒为正。
    """
    expr = hex_to_uint64_expression('"H"', HEX_TO_UINT64_TO_NUMBER)

    assert expr.count("'XXXXXXXX'") == 2
    assert "X" * 16 not in expr
    assert 'SUBSTR("H", 1, 8)' in expr
    assert 'SUBSTR("H", 9, 8)' in expr


def test_nested_mode_is_kept_as_fallback() -> None:
    """兜底写法不依赖格式模型,X 不可用时仍要能算对。"""
    expr = hex_to_uint64_expression('"H"', HEX_TO_UINT64_NESTED)

    assert "TO_NUMBER" not in expr
    assert expr.count("INSTR") == 16


@pytest.mark.parametrize("level", [CompareHashLevel.AGGREGATE, CompareHashLevel.ROW])
def test_plan_sql_shrinks_with_to_number_mode(level: CompareHashLevel) -> None:
    nested = build_dm_compare_hash_plan(_request(level), hex_to_uint64_mode=HEX_TO_UINT64_NESTED)
    to_number = build_dm_compare_hash_plan(
        _request(level), hex_to_uint64_mode=HEX_TO_UINT64_TO_NUMBER
    )

    assert nested.sql is not None
    assert to_number.sql is not None
    assert to_number.sql.count("SUBSTR") == 2
    assert nested.sql.count("SUBSTR") == 16
    assert len(to_number.sql) < len(nested.sql)


def test_plan_defaults_to_nested_when_mode_not_given() -> None:
    """纯计划构造不接触连接,无从判断实例是否支持 X;默认取保守写法。"""
    plan = build_dm_compare_hash_plan(_request())

    assert plan.sql is not None
    assert plan.sql.count("INSTR") == 16


# ──────────────────────────── 探测选型 ────────────────────────────


class _ProbeCursor:
    def __init__(self, behaviour: str, counter: dict[str, int]) -> None:
        self._behaviour = behaviour
        self._counter = counter
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: object = None) -> None:
        del params
        self._counter["queries"] += 1
        if self._behaviour == "supported":
            self._rows = [(0xDEADBEEF, 0xFFFFFFFFFFFFFFFF)]
        elif self._behaviour == "raises":
            raise RuntimeError("invalid number format model")
        elif self._behaviour == "signed":
            # 满宽 X 模型的真实达梦行为:高位翻负
            self._rows = [(0xDEADBEEF, -1)]
        elif self._behaviour == "no_rows":
            self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def close(self) -> None:
        pass


class _ProbeConnection:
    def __init__(self, behaviour: str, counter: dict[str, int]) -> None:
        self._behaviour = behaviour
        self._counter = counter
        self.callTimeout: int | None = None

    def cursor(self) -> _ProbeCursor:
        return _ProbeCursor(self._behaviour, self._counter)

    def close(self) -> None:
        pass


class _ProbeDM:
    def __init__(self, behaviour: str, counter: dict[str, int]) -> None:
        self._behaviour = behaviour
        self._counter = counter

    def connect(self, **kwargs: Any) -> _ProbeConnection:
        del kwargs
        self._counter["connects"] += 1
        return _ProbeConnection(self._behaviour, self._counter)


class _ProbeSecretStore:
    def reveal_secret(self, ref: SecretRef) -> str:
        del ref
        return "pwd"

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


def _probe_adapter(
    behaviour: str,
    extra: dict[str, Any] | None = None,
) -> tuple[DMAdapter, dict[str, int]]:
    counter = {"queries": 0, "connects": 0}
    conn_info = DatasourceConnInfo(
        host="127.0.0.1",
        port=5236,
        username="DATAOPS",
        database="APP",
        password_ref=SecretRef(ref="secret-1", kind=SecretKind.DATASOURCE_PASSWORD),
        db_type=DbType.DM,
        extra=extra or {},
    )
    adapter = DMAdapter(
        conn_info,
        cast(SecretStore, _ProbeSecretStore()),
        dm_module=_ProbeDM(behaviour, counter),
    )
    return adapter, counter


@pytest.mark.parametrize(
    ("behaviour", "expected_mode"),
    [
        ("supported", HEX_TO_UINT64_TO_NUMBER),
        ("raises", HEX_TO_UINT64_NESTED),
        # 达梦真实行为:X 模型被识别,但满宽按 signed 解释 -> 必须判定不可用
        ("signed", HEX_TO_UINT64_NESTED),
        ("no_rows", HEX_TO_UINT64_NESTED),
    ],
)
def test_probe_selects_mode(behaviour: str, expected_mode: str) -> None:
    adapter, _ = _probe_adapter(behaviour)

    adapter.build_compare_hash_query(_request())

    assert adapter.compare_hex_mode == expected_mode


def test_probe_runs_at_most_once_per_adapter() -> None:
    adapter, counter = _probe_adapter("supported")

    for _ in range(5):
        adapter.build_compare_hash_query(_request())

    assert counter["queries"] == 1
    assert counter["connects"] == 1


@pytest.mark.parametrize("forced", [HEX_TO_UINT64_NESTED, HEX_TO_UINT64_TO_NUMBER])
def test_datasource_extra_forces_mode_without_probing(forced: str) -> None:
    """按数据源强制选型,走 extra 这条既有的覆盖通道(与 connect_timeout 同源)。"""
    adapter, counter = _probe_adapter("raises", {"compare_hex_mode": forced})

    adapter.build_compare_hash_query(_request())

    assert adapter.compare_hex_mode == forced
    assert counter["queries"] == 0
    assert counter["connects"] == 0


def test_unknown_extra_mode_falls_back_to_probing() -> None:
    adapter, counter = _probe_adapter("supported", {"compare_hex_mode": "bogus"})

    adapter.build_compare_hash_query(_request())

    assert adapter.compare_hex_mode == HEX_TO_UINT64_TO_NUMBER
    assert counter["queries"] == 1
