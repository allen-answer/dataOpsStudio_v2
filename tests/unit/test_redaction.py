"""R5 红线测试:日志脱敏 processor 强制拦截敏感数据。

每个断言都验证敏感原值在最终输出中**完全不存在**(不仅是被 REDACTED 替换),
即未来谁误改 processor,这些测试会立刻发现密码值漏到日志。

契约 §4 R5 / 设计稿 §9.2.2 — 这是 SecretStore 的最后一道墙。
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator, MutableMapping
from typing import Any

import pytest
import structlog

from app.observability.logging import (
    REDACTED,
    SENSITIVE_KEY_FRAGMENTS,
    _is_sensitive_key,
    _redact_value,
    redact_processor,
)


@pytest.fixture
def capture() -> Iterator[list[dict[str, Any]]]:
    """捕获 structlog 输出供断言。每次测试独立配置 + reset。"""
    captured: list[dict[str, Any]] = []

    def sink(
        logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        captured.append(dict(event_dict))
        return event_dict

    structlog.reset_defaults()
    structlog.configure(
        processors=[redact_processor, sink, structlog.processors.JSONRenderer()],
        logger_factory=structlog.PrintLoggerFactory(file=io.StringIO()),
        cache_logger_on_first_use=False,
    )
    yield captured
    structlog.reset_defaults()


# ─── R5 核心断言:含 password 的 dict 输出必须 REDACTED 且不含明文 ───


def test_password_key_redacted(capture: list[dict[str, Any]]) -> None:
    log = structlog.get_logger()
    log.info("connect", password="hunter2", username="alice")
    out = capture[-1]
    assert out["password"] == REDACTED
    # 整个日志事件序列化后不应含明文(防止任何残留)
    assert "hunter2" not in json.dumps(out)
    # 非敏感字段保留
    assert out["username"] == "alice"


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "passwd",
        "pwd",
        "secret",
        "api_key",
        "apikey",
        "token",
        "mfa_seed",
        "totp_secret",
        "authorization",
        "cookie",
        "ciphertext",
        "private_key",
        "datasource_password",  # 复合
        "AI_API_KEY",  # 大小写
        "Mfa_Totp_Seed",  # 混合
        "encrypted_blob",
        "credential",
    ],
)
def test_sensitive_key_variants_redacted(capture: list[dict[str, Any]], key: str) -> None:
    log = structlog.get_logger()
    log.info("evt", **{key: "leak_value_abc"})
    out = capture[-1]
    assert out[key] == REDACTED
    assert "leak_value_abc" not in json.dumps(out)


# ─── SQL 只记 hash + 长度(L3 不出原文)───


def test_sql_not_logged_raw_only_hash_and_len(capture: list[dict[str, Any]]) -> None:
    log = structlog.get_logger()
    raw = "SELECT * FROM users WHERE password='hunter2'"
    log.info("query", sql=raw)
    out = capture[-1]
    assert "sql" not in out  # 原 key 已被替换
    assert "hunter2" not in json.dumps(out)
    assert "SELECT" not in json.dumps(out)
    assert out["sql_hash"].startswith("sha256:")
    assert out["sql_len"] == len(raw)


# ─── rows / sample 永不进日志(L4 样本数据)───


def test_rows_omitted_completely(capture: list[dict[str, Any]]) -> None:
    log = structlog.get_logger()
    log.info(
        "fetched",
        rows=[{"name": "alice", "ssn": "123456789"}, {"name": "bob"}],
        result_set_id="rs_1",
    )
    out = capture[-1]
    assert "rows" not in out
    assert out["rows_omitted"] is True
    assert "alice" not in json.dumps(out)
    assert "bob" not in json.dumps(out)
    assert "123456789" not in json.dumps(out)
    # 非样本字段保留
    assert out["result_set_id"] == "rs_1"


def test_sample_omitted_completely(capture: list[dict[str, Any]]) -> None:
    log = structlog.get_logger()
    log.info("preview", sample={"col1": "secret_data"})
    out = capture[-1]
    assert "sample" not in out
    assert out["rows_omitted"] is True
    assert "secret_data" not in json.dumps(out)


# ─── 正则兜底:身份证 / 手机号 ───


def test_id_card_pattern_redacted(capture: list[dict[str, Any]]) -> None:
    log = structlog.get_logger()
    log.info("evt", note="user id is 110101199001011234 created")
    out = capture[-1]
    assert "110101199001011234" not in json.dumps(out)
    assert REDACTED in out["note"]


def test_phone_pattern_redacted(capture: list[dict[str, Any]]) -> None:
    log = structlog.get_logger()
    log.info("evt", note="call 13812345678 to confirm")
    out = capture[-1]
    assert "13812345678" not in json.dumps(out)
    assert REDACTED in out["note"]


# ─── 嵌套结构:dict 子 key 命中也要 REDACTED ───


def test_nested_dict_sensitive_key_redacted(capture: list[dict[str, Any]]) -> None:
    log = structlog.get_logger()
    log.info("evt", datasource={"host": "db.example.com", "password": "secret_pwd"})
    out = capture[-1]
    assert "secret_pwd" not in json.dumps(out)
    assert out["datasource"]["host"] == "db.example.com"


def test_list_of_dicts_sensitive_key_redacted(capture: list[dict[str, Any]]) -> None:
    log = structlog.get_logger()
    log.info(
        "evt",
        configs=[
            {"name": "main", "api_key": "leak1"},
            {"name": "fallback", "token": "leak2"},
        ],
    )
    out = capture[-1]
    serialized = json.dumps(out)
    assert "leak1" not in serialized
    assert "leak2" not in serialized


# ─── 非敏感字段保留 ───


def test_non_sensitive_fields_unchanged(capture: list[dict[str, Any]]) -> None:
    log = structlog.get_logger()
    log.info(
        "evt",
        user_id="u_001",
        project_id="p_005",
        duration_ms=1234,
        rows_loaded=5000,
    )
    out = capture[-1]
    assert out["user_id"] == "u_001"
    assert out["project_id"] == "p_005"
    assert out["duration_ms"] == 1234
    assert out["rows_loaded"] == 5000


# ─── 辅助函数单元测试 ───


def test_helper_is_sensitive_key() -> None:
    assert _is_sensitive_key("password")
    assert _is_sensitive_key("PASSWORD")
    assert _is_sensitive_key("api_key")
    assert _is_sensitive_key("MFA_TOTP_SEED")
    assert _is_sensitive_key("datasource_password")
    assert not _is_sensitive_key("username")
    assert not _is_sensitive_key("project_id")
    assert not _is_sensitive_key("duration_ms")


def test_helper_redact_value_primitives_passthrough() -> None:
    assert _redact_value(42) == 42
    assert _redact_value(True) is True
    assert _redact_value(None) is None


def test_helper_redact_value_string_pii() -> None:
    # 身份证
    result = _redact_value("id 110101199001011234 end")
    assert "110101199001011234" not in result
    # 手机
    result = _redact_value("phone 13812345678 ok")
    assert "13812345678" not in result
    # 无 PII 原样
    assert _redact_value("hello world") == "hello world"


def test_helper_redact_value_dict_nested() -> None:
    result = _redact_value(
        {
            "user": "alice",
            "credentials": {"password": "secret"},
            "note": "phone 13812345678",
        }
    )
    serialized = json.dumps(result)
    assert "secret" not in serialized
    assert "13812345678" not in serialized
    assert result["user"] == "alice"


def test_sensitive_fragments_set_contains_baseline() -> None:
    """守护性测试:确保关键敏感片段没被人误删。"""
    must_have = {"password", "secret", "token", "api_key", "mfa", "ciphertext"}
    assert must_have.issubset(SENSITIVE_KEY_FRAGMENTS)
