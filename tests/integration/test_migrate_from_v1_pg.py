"""T8 migrate_from_v1 端到端验收(真 PG,@pytest.mark.integration)。

契约 §5 验收明文项:"1.x 数据能 migrate 进 PG(允许个别字段失败)"。

构造迷你 1.x 合成 fixture(users.json + projects.json + datasources.json +
data/dataops.db 样本)→ run_migration → 断言:
- 条数一致(users / projects / project_members / datasources / jobs / audit_logs)
- datasource 密码"1.x 解密 → 2.0 重加密"往返正确(从 2.0 SecretStore reveal 回明文)
- jobs running 状态迁入标 failed
- 个别字段失败(坏 MFA 密文)记 warning 但用户仍迁入

CI 走 pg-integration job(DATAOPS_TEST_PG_URL)。本地缺 PG → skip。
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

# tests/** 在 ruff TID251 豁免名单内,可直接 import Fernet 构造 1.x 合成密文。
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Engine

from app.db.models import (
    audit_logs,
    datasources,
    job_events,
    jobs,
    metadata,
    project_members,
    projects,
    secret_refs,
    users,
)
from app.domain.secret import SecretKind, SecretRef
from app.infrastructure.secretstore.local_file import LocalFileSecretStore
from app.infrastructure.secretstore.v1_legacy import V1FernetDecryptor
from tools.migrate_from_v1 import (
    MigrationConfigError,
    MigrationOptions,
    _MasterKeyOnlyBootstrap,
    run_migration,
)

pytestmark = pytest.mark.integration


def _pg_engine_or_skip() -> Engine:
    url = os.environ.get("DATAOPS_TEST_PG_URL") or os.environ.get("DATAOPS_DATABASE_URL")
    if not url:
        pytest.skip("migrate_from_v1 PG integration requires DATAOPS_TEST_PG_URL")
    return create_engine(url)


def _clean_db(engine: Engine) -> None:
    metadata.create_all(engine)
    with engine.begin() as conn:
        for table in (
            job_events,
            jobs,
            audit_logs,
            datasources,
            project_members,
            projects,
            users,
            secret_refs,
        ):
            conn.execute(text(f"DELETE FROM {table.name}"))


def _bad_mfa_token() -> str:
    """用一把无关 key 加密 → 迁移用真 key 解密必失败(模拟字段级损坏)。"""
    return "fernet:" + Fernet(Fernet.generate_key()).encrypt(b"x").decode("utf-8")


def _build_v1_fixture(tmp_path: Path, v1_key: bytes) -> Path:
    """造迷你 1.x 实例目录:config/*.json + data/dataops.db。"""
    fernet = Fernet(v1_key)
    cfg = tmp_path / "config"
    cfg.mkdir()
    data = tmp_path / "data"
    data.mkdir()

    mfa_token = "fernet:" + fernet.encrypt(b"JBSWY3DPEHPK3PXP").decode("utf-8")
    users_json = [
        {
            "id": "user-admin",
            "username": "admin",
            "password_hash": "$2b$12$abcdefghijklmnopqrstuv",  # 形态合法的占位 bcrypt
            "role": "admin",
            "created_at": "2026-01-01T00:00:00Z",
            "mfa_secret_encrypted": mfa_token,
            "mfa_enabled": True,
            "mfa_recovery_codes_hashed": ["$2b$12$code1", "$2b$12$code2"],
        },
        {
            "id": "user-bad-mfa",
            "username": "viewer",
            "password_hash": "$2b$12$zzzzzzzzzzzzzzzzzzzzzz",
            "role": "viewer",
            # 用别的 key 加密 → 解密失败 → 字段级 warning,但用户仍迁入
            "mfa_secret_encrypted": _bad_mfa_token(),
        },
    ]
    (cfg / "users.json").write_text(json.dumps(users_json), encoding="utf-8")

    projects_json = [
        {
            "id": "proj-1",
            "name": "Demo",
            "owner_id": "user-admin",
            "members": ["user-admin", "user-bad-mfa"],
            "created_at": "2026-01-02T00:00:00Z",
        }
    ]
    (cfg / "projects.json").write_text(json.dumps(projects_json), encoding="utf-8")

    datasources_json = [
        {
            "id": "ds-1",
            "name": "prod-mysql",
            "db_type": "MySQL",  # 1.x 大小写混合
            "host": "10.0.0.5",
            "port": 3306,
            "database": "appdb",
            "username": "reader",
            "password": "s3cr3t-mysql-pw",  # 明文 → 重加密
            "project_id": "proj-1",
            "environment": "prod",
            "environment_verified": True,
            "extra": {"charset": "utf8mb4", "pool_max_size": 8},
        }
    ]
    (cfg / "datasources.json").write_text(json.dumps(datasources_json), encoding="utf-8")

    # SQLite: jobs + audit_logs
    db = data / "dataops.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE jobs (id TEXT, kind TEXT, status TEXT, started_at TEXT, "
        "finished_at TEXT, cancel_requested INT, payload TEXT, error TEXT)"
    )
    payload = json.dumps({"owner_user_id": "user-admin", "project_id": "proj-1"})
    conn.executemany(
        "INSERT INTO jobs (id, kind, status, payload) VALUES (?, ?, ?, ?)",
        [
            ("job-done", "compare_task", "succeeded", payload),
            ("job-running", "workflow_run", "running", payload),  # → failed
            ("job-orphan", "compare_task", "succeeded", json.dumps({})),  # 无 owner → skip
        ],
    )
    conn.execute(
        "CREATE TABLE audit_logs (id INTEGER PRIMARY KEY, ts TEXT, user_id TEXT, "
        "username TEXT, method TEXT, path TEXT, status INT, request_id TEXT, "
        "resource TEXT, resource_id TEXT, project_id TEXT, extra TEXT)"
    )
    conn.executemany(
        "INSERT INTO audit_logs (ts, user_id, method, path, status, request_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("2026-01-03T10:00:00Z", "user-admin", "POST", "/api/datasources", 200, "req-1"),
            ("2026-01-03T10:01:00Z", "user-admin", "POST", "/api/auth/login", 401, "req-2"),
        ],
    )
    conn.commit()
    conn.close()
    return tmp_path


def _count(engine: Engine, table: object) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()  # type: ignore[arg-type]


def test_migrate_v1_roundtrip_and_counts(tmp_path: Path) -> None:
    engine = _pg_engine_or_skip()
    _clean_db(engine)

    v1_key = Fernet.generate_key()
    master_key = Fernet.generate_key()  # 2.0 新 master key
    source_dir = _build_v1_fixture(tmp_path, v1_key)

    decryptor = V1FernetDecryptor.from_key_material(v1_key)
    bootstrap = _MasterKeyOnlyBootstrap(master_key=master_key)
    store = LocalFileSecretStore(engine, bootstrap)

    report = run_migration(
        source_dir=source_dir,
        engine=engine,
        secret_store=store,
        v1_decryptor=decryptor,
    )

    assert not report.incomplete, report.fatal_error

    # 条数一致
    assert _count(engine, users) == 2
    assert _count(engine, projects) == 1
    # members 含 admin + viewer(owner 已在 members 列表里,不重复补)
    assert _count(engine, project_members) == 2
    assert _count(engine, datasources) == 1
    # jobs: done + running(→failed)迁入;orphan(无 owner)跳过 = 2
    assert _count(engine, jobs) == 2
    assert _count(engine, audit_logs) == 2

    # running → failed
    with engine.connect() as conn:
        status = conn.execute(select(jobs.c.status).where(jobs.c.id == "job-running")).scalar_one()
        assert status == "failed"

        # datasource db_type 小写化 + extra → capability_profile.connection
        ds_row = conn.execute(
            select(
                datasources.c.db_type,
                datasources.c.password_secret_ref,
                datasources.c.capability_profile,
                datasources.c.database_name,
            ).where(datasources.c.id == "ds-1")
        ).one()
        assert ds_row.db_type == "mysql"
        assert ds_row.database_name == "appdb"
        assert ds_row.capability_profile["connection"]["charset"] == "utf8mb4"

        # audit_logs 401 → result=denied
        denied = conn.execute(
            select(audit_logs.c.result).where(audit_logs.c.request_id == "req-2")
        ).scalar_one()
        assert denied == "denied"

    # ★ 密码往返:从 2.0 SecretStore reveal 回明文,应等于 1.x 原明文
    revealed = store.reveal_secret(
        SecretRef(ref=ds_row.password_secret_ref, kind=SecretKind.DATASOURCE_PASSWORD)
    )
    assert revealed == "s3cr3t-mysql-pw"

    # MFA: admin 解密成功 → 有 secret_ref;viewer 坏密文 → 无 ref + warning
    with engine.connect() as conn:
        admin_ref = conn.execute(
            select(users.c.mfa_secret_ref).where(users.c.id == "user-admin")
        ).scalar_one()
        viewer_ref = conn.execute(
            select(users.c.mfa_secret_ref).where(users.c.id == "user-bad-mfa")
        ).scalar_one()
    assert admin_ref is not None
    assert viewer_ref is None
    mfa_plain = store.reveal_secret(SecretRef(ref=admin_ref, kind=SecretKind.MFA_TOTP_SEED))
    assert mfa_plain == "JBSWY3DPEHPK3PXP"

    # 字段级容错:坏 MFA 记 warning,但 viewer 用户仍迁入(allow individual field failure)
    user_warnings = report.tables["users"].warnings
    assert any("mfa_secret decrypt failed" in w for w in user_warnings)
    assert any("recovery codes" in w for w in user_warnings)

    # orphan job 记 skip
    assert report.tables["jobs"].skipped_rows == 1


def _build_global_ds_fixture(tmp_path: Path) -> Path:
    """造迷你 1.x 实例:1 用户 + 0 项目 + 全部全局(project_id 为空)的数据源。

    复刻真实演练里"4/4 数据源全局"的形态。无 SQLite(只验数据源承接)。
    """
    cfg = tmp_path / "config"
    cfg.mkdir()
    users_json = [
        {
            "id": "user-admin",
            "username": "admin",
            "password_hash": "$2b$12$abcdefghijklmnopqrstuv",
            "role": "admin",
        }
    ]
    (cfg / "users.json").write_text(json.dumps(users_json), encoding="utf-8")
    # 没有项目 → 全局数据源唯一去处只能是承接项目。
    (cfg / "projects.json").write_text(json.dumps([]), encoding="utf-8")

    datasources_json = [
        {
            "id": f"ds-global-{i}",
            "name": f"legacy-mysql-{i}",
            "db_type": "MySQL",
            "host": f"10.0.0.{i}",
            "port": 3306,
            "database": f"db{i}",
            "username": "reader",
            "password": f"pw-{i}",
            "project_id": "",  # 1.x 全局可见
            "environment": "prod",
        }
        for i in range(1, 5)  # 4 个,全部全局
    ]
    (cfg / "datasources.json").write_text(json.dumps(datasources_json), encoding="utf-8")
    return tmp_path


def test_migrate_global_datasources_into_synthetic_project(tmp_path: Path) -> None:
    """带 --global-datasource-project:4/4 全局数据源迁入新建承接项目,密码往返正确。"""
    engine = _pg_engine_or_skip()
    _clean_db(engine)

    v1_key = Fernet.generate_key()
    master_key = Fernet.generate_key()
    source_dir = _build_global_ds_fixture(tmp_path)

    decryptor = V1FernetDecryptor.from_key_material(v1_key)
    store = LocalFileSecretStore(engine, _MasterKeyOnlyBootstrap(master_key=master_key))

    report = run_migration(
        source_dir=source_dir,
        engine=engine,
        secret_store=store,
        v1_decryptor=decryptor,
        options=MigrationOptions(
            global_datasource_project="Legacy Global Datasources",
            global_datasource_owner="admin",
        ),
    )
    assert not report.incomplete, report.fatal_error

    # 承接项目计入 projects 报告(migrated +1);owner 进 project_members
    assert report.tables["projects"].migrated == 1
    assert _count(engine, projects) == 1
    assert _count(engine, project_members) == 1
    assert _count(engine, users) == 1
    # 4/4 全局数据源全部迁入(对照旧行为:此前一个都迁不过去)
    assert _count(engine, datasources) == 4
    assert report.tables["datasources"].migrated == 4
    assert report.tables["datasources"].skipped_rows == 0

    with engine.connect() as conn:
        synth = conn.execute(
            select(projects.c.id, projects.c.owner_user_id, projects.c.description).where(
                projects.c.name == "Legacy Global Datasources"
            )
        ).one()
        assert synth.owner_user_id == "user-admin"
        assert synth.description == "migrated: 1.x global datasources"

        # owner membership = owner role
        role = conn.execute(
            select(project_members.c.role).where(
                project_members.c.project_id == synth.id,
                project_members.c.user_id == "user-admin",
            )
        ).scalar_one()
        assert role == "owner"

        # 所有数据源都挂在承接项目下
        ds_proj_ids = conn.execute(select(datasources.c.project_id)).scalars().all()
        assert set(ds_proj_ids) == {synth.id}

        # 密码往返:reveal 回原明文(4 个各重加密)
        refs = conn.execute(
            select(datasources.c.id, datasources.c.password_secret_ref).order_by(datasources.c.id)
        ).all()
    revealed = {
        row.id: store.reveal_secret(
            SecretRef(ref=row.password_secret_ref, kind=SecretKind.DATASOURCE_PASSWORD)
        )
        for row in refs
    }
    assert revealed == {f"ds-global-{i}": f"pw-{i}" for i in range(1, 5)}


def test_migrate_global_datasources_owner_unresolved_raises(tmp_path: Path) -> None:
    """owner 解析不到已迁移用户 → MigrationConfigError(非静默),错误信息列可用 username。"""
    engine = _pg_engine_or_skip()
    _clean_db(engine)

    v1_key = Fernet.generate_key()
    master_key = Fernet.generate_key()
    source_dir = _build_global_ds_fixture(tmp_path)

    store = LocalFileSecretStore(engine, _MasterKeyOnlyBootstrap(master_key=master_key))

    with pytest.raises(MigrationConfigError) as ei:
        run_migration(
            source_dir=source_dir,
            engine=engine,
            secret_store=store,
            v1_decryptor=V1FernetDecryptor.from_key_material(v1_key),
            options=MigrationOptions(
                global_datasource_project="Legacy",
                global_datasource_owner="nonexistent-owner",
            ),
        )
    msg = str(ei.value)
    assert "nonexistent-owner" in msg
    assert "admin" in msg  # 列出可用 username

    # owner 解析失败前没有建承接项目(只有用户已迁)
    assert _count(engine, datasources) == 0
