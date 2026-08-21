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
from typing import Any, cast

import pytest

# R3 test seam:构造 1.x 合成密文,不进入业务代码。
from cryptography.fernet import Fernet  # noqa: TID251

from app.domain.secret import SecretKind, SecretRef
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
    for needle in ("compare_tasks", "sql_templates", "ai_configs", "run_index"):
        assert needle in labels
    assert "workflows" not in labels
    assert "workflow_templates" not in labels
    # .dataops_secret.key 是输入不是目标
    assert any(".dataops_secret.key" in label for label, _ in m._SKIPPED_SOURCES)


# ─── 全局数据源承接项目(--global-datasource-project)──────────────────────────
class _FakeSecretStore:
    """记录 store_secret 调用次数,返回稳定 ref(不连 PG,纯单测用)。"""

    def __init__(self) -> None:
        self.calls = 0

    def store_secret(self, plaintext: str, kind: object) -> SecretRef:
        self.calls += 1
        return SecretRef(ref=f"ref-{self.calls:0>4}", kind=SecretKind.DATASOURCE_PASSWORD)


class _RecordingConn:
    """记录 migrate_datasources 写入的 datasources 行(不连真 DB)。"""

    def __init__(self) -> None:
        self.inserts: list[dict[str, object]] = []

    def execute(self, stmt: object) -> None:
        # migrate_datasources 只对 datasources 做 insert().values(**values)
        compiled = stmt.compile()  # type: ignore[attr-defined]
        self.inserts.append(dict(compiled.params))


def _global_ds_rows() -> list[dict[str, object]]:
    return [
        {
            "id": "ds-global-1",
            "name": "g1",
            "db_type": "MySQL",
            "host": "h1",
            "port": 3306,
            "username": "u",
            "password": "pw1",
            "project_id": "",  # 1.x 全局
        },
        {
            "id": "ds-global-2",
            "name": "g2",
            "db_type": "oracle",
            "host": "h2",
            "port": 1521,
            "username": "u",
            "password": "pw2",
            "project_id": "",  # 1.x 全局
        },
    ]


def test_has_global_datasource_detects_empty_project_id() -> None:
    assert m._has_global_datasource(_global_ds_rows()) is True
    assert m._has_global_datasource([{"id": "x", "project_id": "proj-1"}]) is False
    assert m._has_global_datasource([]) is False


def test_migrate_datasources_without_flag_skips_global() -> None:
    """不带旗标:全局数据源全部跳过(现行为不回归)。"""
    report = m.MigrationReport()
    conn = _RecordingConn()
    store = _FakeSecretStore()
    attached = m.migrate_datasources(
        conn,  # type: ignore[arg-type]
        _global_ds_rows(),
        known_project_ids=set(),
        secret_store=store,  # type: ignore[arg-type]
        report=report,
        global_project_id=None,
    )
    assert attached == 0
    assert conn.inserts == []  # 没写任何行
    assert report.table("datasources").migrated == 0
    assert report.table("datasources").skipped_rows == 2
    assert store.calls == 0  # 跳过的行不应重加密密码


def test_migrate_datasources_with_flag_routes_global_to_project() -> None:
    """带旗标:全局数据源全部挂入承接项目,密码各重加密一次。"""
    report = m.MigrationReport()
    conn = _RecordingConn()
    store = _FakeSecretStore()
    attached = m.migrate_datasources(
        conn,  # type: ignore[arg-type]
        _global_ds_rows(),
        known_project_ids=set(),
        secret_store=store,  # type: ignore[arg-type]
        report=report,
        global_project_id="synthetic-proj-id",
    )
    assert attached == 2
    assert report.table("datasources").migrated == 2
    assert report.table("datasources").skipped_rows == 0
    assert store.calls == 2  # 两个全局数据源各重加密一次
    assert {row["project_id"] for row in conn.inserts} == {"synthetic-proj-id"}


def test_resolve_global_owner_success_and_failure() -> None:
    opts = m.MigrationOptions(global_datasource_project="Legacy", global_datasource_owner="admin")
    assert m._resolve_global_owner(opts, {"admin": "u-admin"}) == "u-admin"

    missing = m.MigrationOptions(
        global_datasource_project="Legacy", global_datasource_owner="ghost"
    )
    with pytest.raises(m.MigrationConfigError) as ei:
        m._resolve_global_owner(missing, {"admin": "u-admin", "viewer": "u-view"})
    # 错误信息指明可用 username
    msg = str(ei.value)
    assert "ghost" in msg
    assert "admin" in msg and "viewer" in msg


# ─── Workflow 2.4 迁移───────────────────────────────────────────────────────
def _workflow_rows() -> list[dict[str, object]]:
    return [
        {
            "id": "wf-1",
            "name": "Daily Checks",
            "project_id": "proj-1",
            "owner": "admin",
            "status": "active",
            "schedule_cron": "0 2 * * *",
            "nodes": [
                {
                    "id": "compare",
                    "type": "compare",
                    "name": "Compare",
                    "config": {
                        "task_id": "task-1",
                        "source_sql_override": "select 1",
                    },
                },
                {
                    "id": "lineage",
                    "type": "lineage",
                    "config": {"sql": "insert into b select * from a"},
                    "depends_on": ["compare"],
                    "when": "nodes.compare.summary.diff_rows == 0",
                },
                {
                    "id": "excel",
                    "type": "excel_export",
                    "config": {"sheets": [{"node_id": "compare", "dataset": "summary"}]},
                    "depends_on": ["lineage"],
                },
            ],
        }
    ]


def test_migrate_workflows_maps_supported_nodes_edges_and_when() -> None:
    report = m.MigrationReport()
    conn = _RecordingConn()

    m.migrate_workflows(
        conn,  # type: ignore[arg-type]
        _workflow_rows(),
        known_project_ids={"proj-1"},
        known_user_ids={"user-admin"},
        username_to_id={"admin": "user-admin"},
        report=report,
    )

    assert report.table("workflows").migrated == 1
    assert len(conn.inserts) == 1
    row = conn.inserts[0]
    dag = cast(dict[str, Any], row["dag_jsonb"])
    assert row["created_by"] == "user-admin"
    assert row["schedule_cron"] == "0 2 * * *"
    assert row["schedule_enabled"] is True
    assert [node["job_kind"] for node in dag["nodes"]] == [
        "compare_run",
        "lineage_analyze",
        "export_excel",
    ]
    assert dag["edges"] == [
        {
            "source": "compare",
            "target": "lineage",
            "trigger": "success",
            "when": None,
            "is_default": False,
        },
        {
            "source": "lineage",
            "target": "excel",
            "trigger": "success",
            "when": None,
            "is_default": False,
        },
    ]
    assert dag["nodes"][1]["when"] == "nodes.compare.summary.diff_rows == 0"
    assert dag["nodes"][0]["payload"]["task_id"] == "task-1"
    assert dag["nodes"][0]["payload"]["legacy_node_type"] == "compare"
    assert any(
        "legacy config preserved in payload" in item for item in report.table("workflows").warnings
    )


def test_migrate_workflows_http_node_reports_forbidden_without_insert() -> None:
    report = m.MigrationReport()
    conn = _RecordingConn()
    rows = _workflow_rows()
    rows[0]["nodes"] = [
        {"id": "notify", "type": "http", "config": {"url": "https://example.invalid"}}
    ]

    with pytest.raises(m.WorkflowMigrationError):
        m.migrate_workflows(
            conn,  # type: ignore[arg-type]
            rows,
            known_project_ids={"proj-1"},
            known_user_ids={"user-admin"},
            username_to_id={"admin": "user-admin"},
            report=report,
        )

    assert conn.inserts == []
    failures = report.table("workflows").failures
    assert any("forbidden:" in item and "node id=notify" in item for item in failures)


def test_migrate_workflows_params_node_reports_unmappable_without_insert() -> None:
    report = m.MigrationReport()
    conn = _RecordingConn()
    rows = _workflow_rows()
    rows[0]["nodes"] = [{"id": "params", "type": "params", "config": {"parameters": []}}]

    with pytest.raises(m.WorkflowMigrationError):
        m.migrate_workflows(
            conn,  # type: ignore[arg-type]
            rows,
            known_project_ids={"proj-1"},
            known_user_ids={"user-admin"},
            username_to_id={"admin": "user-admin"},
            report=report,
        )

    assert conn.inserts == []
    failures = report.table("workflows").failures
    assert any("unmappable:" in item and "node id=params" in item for item in failures)


def test_migrate_workflows_cycle_reports_validation_failed_without_insert() -> None:
    report = m.MigrationReport()
    conn = _RecordingConn()
    rows = _workflow_rows()
    rows[0]["nodes"] = [
        {"id": "a", "type": "compare", "config": {"task_id": "task-1"}, "depends_on": ["b"]},
        {"id": "b", "type": "lineage", "config": {"sql": "select 1"}, "depends_on": ["a"]},
    ]

    with pytest.raises(m.WorkflowMigrationError):
        m.migrate_workflows(
            conn,  # type: ignore[arg-type]
            rows,
            known_project_ids={"proj-1"},
            known_user_ids={"user-admin"},
            username_to_id={"admin": "user-admin"},
            report=report,
        )

    assert conn.inserts == []
    failures = report.table("workflows").failures
    assert any("validation_failed:" in item and "cycle_detected" in item for item in failures)


def test_migrate_workflow_templates_global_migration() -> None:
    report = m.MigrationReport()
    conn = _RecordingConn()
    rows = [
        {
            "id": "tpl-1",
            "name": "Compare Template",
            "description": "template desc",
            "workflow": {
                "name": "Inner",
                "owner": "admin",
                "nodes": [
                    {
                        "id": "compare",
                        "type": "compare",
                        "config": {"task_id": "task-1"},
                    }
                ],
            },
        }
    ]

    m.migrate_workflow_templates(
        conn,  # type: ignore[arg-type]
        rows,
        known_user_ids={"user-admin"},
        username_to_id={"admin": "user-admin"},
        report=report,
    )

    assert report.table("workflow_templates").migrated == 1
    assert len(conn.inserts) == 1
    row = conn.inserts[0]
    assert row["id"] == "tpl-1"
    assert row["name"] == "Compare Template"
    assert row["description"] == "template desc"
    assert row["created_by"] == "user-admin"
    dag = cast(dict[str, Any], row["dag_jsonb"])
    assert dag["nodes"][0]["job_kind"] == "compare_run"
