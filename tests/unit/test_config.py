"""app/config.py smoke tests。

验证默认配置可加载、env 覆盖生效、Bootstrap 路径不持值只持路径。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import (
    ApiConfig,
    BootstrapPaths,
    Form,
    JobBackendKind,
    MetadataDbConfig,
    Settings,
    load_settings,
)


def test_load_settings_defaults() -> None:
    s = load_settings()
    assert s.form == Form.DEV
    assert s.job_backend == JobBackendKind.POSTGRES
    # PG 默认端口 15432 —— 与 1.x 隔离
    assert s.db.port == 15432
    # API 默认 8020 / metrics 8021 —— 避开 1.x 8010 / daily-report-bot 8000
    assert s.api.port == 8020
    assert s.api.metrics_port == 8021
    assert s.api.enable_docs is None
    # 慢首行 OLAP 查询优先避免误杀;故障恢复宁可晚几分钟。
    assert s.worker.heartbeat_timeout_seconds == 600
    assert s.worker.poll_interval_seconds == 0.1
    assert s.worker.cancel_check_row_interval == 5000
    # AI 默认关(R5 + 形态默认)
    assert s.ai.enabled is False
    assert s.ai.max_auto_egress_level == 0


def test_bootstrap_paths_are_paths_only_no_values() -> None:
    """R2 / R8 红线:Bootstrap 配置只持路径,不持 secret 值。"""
    bp = BootstrapPaths()
    assert isinstance(bp.master_key_file, Path)
    assert isinstance(bp.pg_app_password_file, Path)
    assert isinstance(bp.pg_superuser_password_file, Path)
    assert isinstance(bp.jwt_secret_file, Path)
    assert isinstance(bp.license_file, Path)
    # 默认路径在 config/ 下,这些在 .gitignore 精确忽略
    assert bp.master_key_file.as_posix().startswith("config/")
    assert bp.pg_app_password_file.as_posix().startswith("config/secrets/")
    assert bp.jwt_secret_file.as_posix().startswith("config/secrets/")


def test_metadata_db_config_no_password_field() -> None:
    """R2 红线:MetadataDbConfig 禁有 password 字段。"""
    db = MetadataDbConfig()
    fields = MetadataDbConfig.model_fields
    assert "password" not in fields
    assert "passwd" not in fields
    assert "pwd" not in fields
    # 但有 host/port/user/database/pool_* 等非敏感字段
    assert db.host == "127.0.0.1"
    assert db.user == "dataops"


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """env 覆盖生效:DATAOPS_PG_PORT / DATAOPS_API_PORT / DATAOPS_FORM。"""
    monkeypatch.setenv("DATAOPS_FORM", "portable")
    monkeypatch.setenv("DATAOPS_PG_PORT", "25432")
    monkeypatch.setenv("DATAOPS_API_PORT", "9020")
    s = Settings()
    assert s.form == Form.PORTABLE
    assert s.db.port == 25432
    assert s.api.port == 9020


def test_form_enum_values() -> None:
    """四个形态(hosted/onprem_docker/portable/dev),不可缺。"""
    expected = {"hosted", "onprem_docker", "portable", "dev"}
    assert {f.value for f in Form} == expected


def test_api_config_defaults_avoid_v1_ports() -> None:
    """端口规划:8020/8021 与 daily-server 上 1.x 8010 / daily-report-bot 8000 隔离。"""
    api = ApiConfig()
    forbidden = {80, 443, 3307, 5432, 8000, 8010, 8080}
    assert api.port not in forbidden
    assert api.metrics_port not in forbidden
