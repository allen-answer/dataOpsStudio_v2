"""T8 migrate_from_v1 单元测试(不依赖 PG)。

覆盖:
- V1FernetDecryptor 解密往返(1.x Fernet 字段 → 明文),两种 key 形态
- 1.x 兼容行为:老明文(无 fernet: 前缀)原样返回 / 空值 → 空
- v1_sources 读取(JSON list / SQLite 表 / 缺文件回落)
- 纯映射 helper(db_type / environment / status / datetime 解析)
- 跳过源清单含 2.1+ 目标表(不迁)

真正的"条数一致 + 密码往返"端到端在 tests/integration/test_migrate_from_v1_pg.py
(需要真 PG,@pytest.mark.integration)。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

# tests/** 在 ruff TID251 豁免名单内,可直接 import Fernet 构造 1.x 测试密文。
from cryptography.fernet import Fernet

from app.infrastructure.secretstore.v1_legacy import V1FernetDecryptor, V1SecretDecryptError
from tools import migrate_from_v1 as m
from tools.v1_sources import load_json_list, open_sqlite, read_sqlite_rows


# ─── V1FernetDecryptor ──────────────────────────────────────────────────────
def test_decryptor_roundtrip_with_generated_key() -> None:
    key = Fernet.generate_key()
    token = Fernet(key).encrypt(b"totp-seed-ABC123").decode("utf-8")
    decryptor = V1FernetDecryptor.from_key_material(key)
    assert decryptor.decrypt(f"fernet:{token}") == "totp-seed-ABC123"


def test_decryptor_env_string_key_form() -> None:
    # 1.x env 形态:任意字符串 → sha256 派生 Fernet key。
    import base64
    import hashlib

    secret = "my-env-secret-string"
    derived = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    token = Fernet(derived).encrypt(b"sk-test-apikey").decode("utf-8")

    decryptor = V1FernetDecryptor.from_key_material(secret)
    assert decryptor.decrypt(f"fernet:{token}") == "sk-test-apikey"


def test_decryptor_plaintext_without_prefix_returned_asis() -> None:
    decryptor = V1FernetDecryptor.from_key_material(Fernet.generate_key())
    assert decryptor.decrypt("legacy-plaintext-no-prefix") == "legacy-plaintext-no-prefix"


def test_decryptor_empty_returns_empty() -> None:
    decryptor = V1FernetDecryptor.from_key_material(Fernet.generate_key())
    assert decryptor.decrypt("") == ""


def test_decryptor_wrong_key_raises() -> None:
    token = Fernet(Fernet.generate_key()).encrypt(b"x").decode("utf-8")
    other = V1FernetDecryptor.from_key_material(Fernet.generate_key())
    with pytest.raises(V1SecretDecryptError):
        other.decrypt(f"fernet:{token}")


def test_decryptor_empty_key_material_raises() -> None:
    with pytest.raises(V1SecretDecryptError):
        V1FernetDecryptor.from_key_material("   ")


def test_decryptor_from_key_file(tmp_path: Path) -> None:
    key = Fernet.generate_key()
    key_file = tmp_path / ".dataops_secret.key"
    key_file.write_bytes(key)
    token = Fernet(key).encrypt(b"file-key-secret").decode("utf-8")
    decryptor = V1FernetDecryptor.from_key_file(key_file)
    assert decryptor.decrypt(f"fernet:{token}") == "file-key-secret"


def test_decryptor_missing_key_file_raises(tmp_path: Path) -> None:
    with pytest.raises(V1SecretDecryptError):
        V1FernetDecryptor.from_key_file(tmp_path / "nope.key")


# ─── v1_sources ─────────────────────────────────────────────────────────────
def test_load_json_list_missing_returns_empty(tmp_path: Path) -> None:
    assert load_json_list(tmp_path, "users.json") == []


def test_load_json_list_reads_array(tmp_path: Path) -> None:
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "users.json").write_text(json.dumps([{"id": "u1"}, "skip-non-dict"]), encoding="utf-8")
    rows = load_json_list(tmp_path, "users.json")
    assert rows == [{"id": "u1"}]


def test_open_sqlite_missing_yields_none(tmp_path: Path) -> None:
    with open_sqlite(tmp_path) as conn:
        assert conn is None


def test_read_sqlite_rows_and_missing_table(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = data_dir / "dataops.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE jobs (id TEXT, status TEXT)")
    conn.execute("INSERT INTO jobs VALUES ('j1', 'running')")
    conn.commit()
    conn.close()

    with open_sqlite(tmp_path) as ro:
        assert ro is not None
        assert read_sqlite_rows(ro, "jobs") == [{"id": "j1", "status": "running"}]
        # 缺表 → 空列表(不抛)
        assert read_sqlite_rows(ro, "audit_logs") == []


# ─── 纯映射 helper ──────────────────────────────────────────────────────────
def test_db_type_map_lowercases_1x_mixed_case() -> None:
    assert m._DB_TYPE_MAP.get("mysql") == "mysql"
    assert m._DB_TYPE_MAP.get("oracle") == "oracle"
    assert m._DB_TYPE_MAP.get("db2") == "db2"


def test_environment_map_fallbacks() -> None:
    assert m._ENVIRONMENT_MAP["unknown"] == "dev"
    assert m._ENVIRONMENT_MAP["prod"] == "prod"
    assert m._ENVIRONMENT_MAP.get("nonexistent") is None  # caller falls back to dev


def test_parse_dt_handles_iso_and_blank() -> None:
    assert m._parse_dt("") is None
    assert m._parse_dt(None) is None
    assert m._parse_dt("not-a-date") is None
    dt = m._parse_dt("2026-01-02T03:04:05Z")
    assert dt is not None
    assert dt.year == 2026 and dt.tzinfo is not None


def test_skipped_sources_lists_21_plus_tables() -> None:
    labels = " ".join(label for label, _ in m._SKIPPED_SOURCES)
    for needle in ("compare_tasks", "workflows", "sql_templates", "ai_configs", "run_index"):
        assert needle in labels
    # .dataops_secret.key 是输入不是目标
    assert any(".dataops_secret.key" in label for label, _ in m._SKIPPED_SOURCES)
