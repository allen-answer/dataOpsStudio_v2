"""migrate_from_v1.py —— DataOpsStudio 1.x → 2.0 数据迁移(契约 §5 / 设计稿 §5.4)。

目标:把 1.x 的 config/*.json + data/dataops.db 迁进 2.0 统一 PostgreSQL。

用法:
    python -m tools.migrate_from_v1 \
        --source <v1_dir> \
        --target <pg_dsn> \
        --v1-secret-key <path/to/.dataops_secret.key> \
        --master-key-file <path/to/2.0/.secret_master.key>

边界(严守):
- 只迁 2.0 已建表的源数据(以 app/db/models.py 实测为准):
    users / projects(+project_members) / datasources / jobs / audit_logs
- 2.1+ 才有的目标表(compare_tasks / workflows / scenario_templates /
  sql_templates / ai_configs / asset_aspects / refresh_tokens / ...)2.0.0
  骨架未建,**不迁、不建 migration**,只在报告里列"跳过 + 原因"。
- audit_logs / jobs 只从 SQLite 迁,不重复读 jsonl/json(1.x 启动已迁进 SQLite)。
- jobs 中 running 状态迁入时标 failed。
- datasource 明文 password → 2.0 SecretStore 重加密 → secret_ref(走 secretstore 模块)。
- 1.x Fernet 字段(mfa_secret_encrypted)→ V1FernetDecryptor 解密 → SecretStore 重加密。

容错(契约"允许个别字段失败"):
- 单字段异常 → 记 warning 继续(不是吞错,报告里逐条列)。
- 整行无法满足 2.0 NOT NULL / FK → 记 skip + 原因,不写半成品行。
- 迁移失败保留已写入部分,但报告明确标 incomplete(防半成品被当成功)。

R5:迁移日志走 structlog 强制脱敏 processor,零明文密码。
R3:Fernet 解密在 app/infrastructure/secretstore/v1_legacy.py(本文件不碰 Fernet)。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import create_engine, insert
from sqlalchemy.engine import Connection, Engine

from app.db.models import (
    audit_logs,
    datasources,
    jobs,
    project_members,
    projects,
    users,
)
from app.domain.secret import SecretKind
from app.infrastructure.bootstrap.protocol import BootstrapSecrets
from app.infrastructure.secretstore.local_file import LocalFileSecretStore
from app.infrastructure.secretstore.protocol import SecretStore
from app.infrastructure.secretstore.v1_legacy import V1FernetDecryptor, V1SecretDecryptError
from app.observability.logging import configure_logging
from tools.v1_sources import (
    load_json_list,
    open_sqlite,
    read_sqlite_rows,
)

logger = structlog.get_logger("migrate_from_v1")

# 1.x DatabaseType 字面值 → 2.0 DbType。1.x 用大小写混合("MySQL"),2.0 用小写。
_DB_TYPE_MAP: dict[str, str] = {
    "mysql": "mysql",
    "oracle": "oracle",
    "dm": "dm",
    "db2": "db2",
    "postgresql": "postgresql",
}

# 1.x environment 字面值 → 2.0。1.x: unknown/sandbox/staging/prod。
# 2.0 datasources.environment 默认 dev;做保守映射,无对应回落 dev。
_ENVIRONMENT_MAP: dict[str, str] = {
    "unknown": "dev",
    "sandbox": "dev",
    "staging": "staging",
    "prod": "prod",
    "production": "prod",
    "dev": "dev",
}

# 2.0.0 骨架未建表的 1.x 源(设计稿 §5.4 含 2.1+ 目标表)。逐项列"跳过 + 原因"。
_SKIPPED_SOURCES: tuple[tuple[str, str], ...] = (
    ("config/tasks.json → compare_tasks", "2.0.0 骨架未建 compare_tasks 表(2.2 Compare)"),
    ("config/workflows.json → workflows", "2.0.0 骨架未建 workflows 表(2.5 Workflow)"),
    (
        "config/workflow_templates.json → workflow_templates",
        "2.0.0 骨架未建 workflow_templates 表(2.5 Workflow)",
    ),
    (
        "config/scenarios/*.yml → scenario_templates",
        "2.0.0 骨架未建 scenario_templates 表(2.3 Scenario)",
    ),
    (
        "config/sql_templates.json → sql_templates",
        "2.0.0 骨架未建 sql_templates 表(2.1 SQL Workspace)",
    ),
    ("config/lineage_ai.json → ai_configs", "2.0.0 骨架未建 ai_configs 表(AI Gateway 仅壳)"),
    ("config/asset_aspects.yml → aspect 类型定义", "2.0.0 骨架未迁资产分类定义"),
    ("SQLite asset_aspects → asset_aspects", "2.0.0 骨架未建 asset_aspects 表"),
    (
        "SQLite asset_aspect_history → asset_aspect_history",
        "2.0.0 骨架未建 asset_aspect_history 表",
    ),
    ("SQLite refresh_tokens → refresh_tokens", "2.0.0 骨架未建 refresh_tokens 表"),
    ("SQLite revoked_tokens → revoked_tokens", "2.0.0 骨架未建 revoked_tokens 表"),
    ("SQLite download_nonces", "一次性 nonce,设计稿明示可丢"),
    ("SQLite slow_sql_plans → slow_sql_plans", "2.0.0 骨架未建 slow_sql_plans 表"),
    ("SQLite run_index → run_index", "2.0.0 骨架未建 run_index 表"),
    (
        "results/<run_id>/ 结果文件 + run_index 反向索引",
        "依赖 run_index 表(未建);结果文件保留原路径",
    ),
    ("lineage 解析结果", "设计稿明示不迁(1.x 不持久化,每次重算)"),
    ("config/.dataops_secret.key", "迁移输入(解密旧字段),不是迁移目标"),
    ("config/metadata_cache/*.json", "sqlide 元数据缓存,2.0 重建即可"),
    ("User.mfa_recovery_codes_hashed", "2.0.0 users 表无 recovery codes 列(后续版本)"),
)


# ─────────────────────────────────────────────────────────────────────────────
# 报告结构
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class TableReport:
    table: str
    migrated: int = 0
    skipped_rows: int = 0
    warnings: list[str] = field(default_factory=list)
    skips: list[str] = field(default_factory=list)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def skip(self, msg: str) -> None:
        self.skipped_rows += 1
        self.skips.append(msg)


@dataclass
class MigrationReport:
    tables: dict[str, TableReport] = field(default_factory=dict)
    secrets_created: int = 0
    skipped_sources: list[tuple[str, str]] = field(default_factory=list)
    incomplete: bool = False
    fatal_error: str | None = None

    def table(self, name: str) -> TableReport:
        return self.tables.setdefault(name, TableReport(table=name))


# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap(2.0 master key)—— 只为构造 SecretStore;不连 PG,不存 PG。
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _MasterKeyOnlyBootstrap(BootstrapSecrets):
    """迁移工具只需要 master key 来构造 SecretStore;其余 Bootstrap 不在此用。"""

    master_key: bytes

    def get_master_key(self) -> bytes:
        return self.master_key

    def get_pg_app_password(self) -> str:  # pragma: no cover - 迁移工具不用
        raise NotImplementedError("migration tool does not use pg_app_password")

    def get_pg_superuser_password(self) -> str:  # pragma: no cover
        raise NotImplementedError("migration tool does not use pg_superuser_password")

    def get_jwt_secret(self) -> str:  # pragma: no cover
        raise NotImplementedError("migration tool does not use jwt_secret")

    def get_license_file(self) -> bytes | None:  # pragma: no cover
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 字段工具
# ─────────────────────────────────────────────────────────────────────────────
def _parse_dt(raw: object) -> datetime | None:
    """1.x created_at / started_at 是字符串(ISO 或空)。解析失败 → None(用 DB 默认)。"""
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        # 兼容尾缀 Z。
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _coalesce_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


# ─────────────────────────────────────────────────────────────────────────────
# 各表迁移
# ─────────────────────────────────────────────────────────────────────────────
def migrate_users(
    conn: Connection,
    rows: Sequence[Mapping[str, Any]],
    secret_store: SecretStore,
    v1_decryptor: V1FernetDecryptor,
    report: MigrationReport,
) -> set[str]:
    """迁 config/users.json → users。返回成功迁入的 user id 集合(供 FK 校验)。"""
    rep = report.table("users")
    migrated_ids: set[str] = set()
    for row in rows:
        user_id = _coalesce_str(row.get("id"))
        username = _coalesce_str(row.get("username"))
        password_hash = _coalesce_str(row.get("password_hash"))
        if not (user_id and username and password_hash):
            rep.skip(f"user missing id/username/password_hash (id={user_id or '<none>'})")
            continue

        mfa_secret_ref: str | None = None
        encrypted = _coalesce_str(row.get("mfa_secret_encrypted"))
        if encrypted:
            try:
                plaintext = v1_decryptor.decrypt(encrypted)
                if plaintext:
                    ref = secret_store.store_secret(plaintext, SecretKind.MFA_TOTP_SEED)
                    report.secrets_created += 1
                    mfa_secret_ref = ref.ref
            except V1SecretDecryptError:
                # 字段级失败:记 warning 继续(不吞错,不中断整库迁移)。
                rep.warn(f"mfa_secret decrypt failed for user id={user_id}; migrated without MFA")

        recovery = row.get("mfa_recovery_codes_hashed")
        if isinstance(recovery, list) and recovery:
            rep.warn(
                f"user id={user_id} has {len(recovery)} recovery codes; "
                "2.0.0 users table has no column for them (skipped)"
            )

        values: dict[str, Any] = {
            "id": user_id,
            "username": username,
            "password_hash": password_hash,
            "mfa_secret_ref": mfa_secret_ref,
            "role": _coalesce_str(row.get("role"), "viewer") or "viewer",
        }
        created = _parse_dt(row.get("created_at"))
        if created is not None:
            values["created_at"] = created
            values["updated_at"] = created
        conn.execute(insert(users).values(**values))
        migrated_ids.add(user_id)
        rep.migrated += 1
    return migrated_ids


def migrate_projects(
    conn: Connection,
    rows: Sequence[Mapping[str, Any]],
    known_user_ids: set[str],
    report: MigrationReport,
) -> set[str]:
    """迁 config/projects.json → projects + project_members。返回成功 project id 集合。"""
    rep = report.table("projects")
    mem_rep = report.table("project_members")
    migrated_ids: set[str] = set()
    for row in rows:
        project_id = _coalesce_str(row.get("id"))
        name = _coalesce_str(row.get("name"))
        owner_id = _coalesce_str(row.get("owner_id"))
        if not (project_id and name):
            rep.skip(f"project missing id/name (id={project_id or '<none>'})")
            continue
        if not owner_id or owner_id not in known_user_ids:
            # projects.owner_user_id 是 NOT NULL FK → users;owner 缺失则整行无法写。
            owner_label = owner_id or "<none>"
            rep.skip(f"project id={project_id} owner_id not migrated/missing (owner={owner_label})")
            continue

        values: dict[str, Any] = {
            "id": project_id,
            "name": name,
            "owner_user_id": owner_id,
            "description": _coalesce_str(row.get("description")) or None,
        }
        created = _parse_dt(row.get("created_at"))
        if created is not None:
            values["created_at"] = created
            values["updated_at"] = created
        conn.execute(insert(projects).values(**values))
        migrated_ids.add(project_id)
        rep.migrated += 1

        # members(User.id 数组)拆成 project_members 关联表。owner 标 owner 角色。
        members = row.get("members")
        member_ids: list[str] = members if isinstance(members, list) else []
        seen: set[str] = set()
        for member_id in member_ids:
            if not isinstance(member_id, str) or not member_id:
                continue
            if member_id in seen:
                continue
            seen.add(member_id)
            if member_id not in known_user_ids:
                mem_rep.skip(
                    f"project id={project_id} member {member_id} not a migrated user (skipped)"
                )
                continue
            conn.execute(
                insert(project_members).values(
                    project_id=project_id,
                    user_id=member_id,
                    role="owner" if member_id == owner_id else "member",
                )
            )
            mem_rep.migrated += 1
        # owner 不在 members 列表里时,补一条 owner membership。
        if owner_id not in seen:
            conn.execute(
                insert(project_members).values(
                    project_id=project_id, user_id=owner_id, role="owner"
                )
            )
            mem_rep.migrated += 1
    return migrated_ids


def migrate_datasources(
    conn: Connection,
    rows: Sequence[Mapping[str, Any]],
    known_project_ids: set[str],
    secret_store: SecretStore,
    report: MigrationReport,
) -> None:
    """迁 config/datasources.json → datasources。明文 password → SecretStore → secret_ref。"""
    rep = report.table("datasources")
    for row in rows:
        ds_id = _coalesce_str(row.get("id"))
        name = _coalesce_str(row.get("name"))
        if not (ds_id and name):
            rep.skip(f"datasource missing id/name (id={ds_id or '<none>'})")
            continue

        raw_db_type = _coalesce_str(row.get("db_type")).lower()
        db_type = _DB_TYPE_MAP.get(raw_db_type)
        if db_type is None:
            rep.skip(f"datasource id={ds_id} unknown db_type={raw_db_type!r}")
            continue

        # project_id:1.x 空字符串 = 全局可见;2.0 datasources.project_id 是 NOT NULL FK。
        project_id = _coalesce_str(row.get("project_id"))
        if not project_id or project_id not in known_project_ids:
            rep.skip(
                f"datasource id={ds_id} project_id not migrated/missing "
                f"(project={project_id or '<global>'})"
            )
            continue

        host = _coalesce_str(row.get("host"))
        port = row.get("port")
        username = _coalesce_str(row.get("username"))
        if not host or not isinstance(port, int):
            rep.skip(f"datasource id={ds_id} missing host/port")
            continue

        # 明文 password → 2.0 SecretStore 重加密 → secret_ref(R2/R3:不在本工具持明文落盘)。
        password = _coalesce_str(row.get("password"))
        try:
            ref = secret_store.store_secret(password, SecretKind.DATASOURCE_PASSWORD)
            report.secrets_created += 1
        except Exception:
            rep.skip(f"datasource id={ds_id} password re-encryption failed")
            continue

        extra = row.get("extra")
        capability_profile: dict[str, Any] = {}
        if isinstance(extra, dict) and extra:
            capability_profile["connection"] = extra

        raw_env = _coalesce_str(row.get("environment")).lower()
        environment = _ENVIRONMENT_MAP.get(raw_env, "dev")
        if raw_env and raw_env not in _ENVIRONMENT_MAP:
            rep.warn(f"datasource id={ds_id} unknown environment={raw_env!r} → 'dev'")

        values: dict[str, Any] = {
            "id": ds_id,
            "project_id": project_id,
            "name": name,
            "db_type": db_type,
            "host": host,
            "port": port,
            "username": username,
            "database_name": _coalesce_str(row.get("database")) or None,
            "password_secret_ref": ref.ref,
            "environment": environment,
            "environment_verified": bool(row.get("environment_verified", False)),
            "capability_profile": capability_profile,
        }
        conn.execute(insert(datasources).values(**values))
        rep.migrated += 1


def migrate_jobs(
    conn: Connection,
    rows: Sequence[Mapping[str, Any]],
    known_user_ids: set[str],
    known_project_ids: set[str],
    report: MigrationReport,
) -> None:
    """迁 SQLite jobs → jobs。running 标 failed。owner/project/timeout 不全的行跳过。"""
    rep = report.table("jobs")
    for row in rows:
        job_id = _coalesce_str(row.get("id"))
        if not job_id:
            rep.skip("job missing id")
            continue

        payload = row.get("payload")
        if isinstance(payload, str):
            # 1.x payload 是 JSON 序列化字符串;解析失败按空 dict。
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        if not isinstance(payload, dict):
            payload = {}

        # 2.0 jobs 是 NOT NULL FK(owner_user_id / project_id),1.x jobs 表本身无这两列,
        # 只能从 payload 捞;捞不到 / 不在已迁集合 → 跳过(不写违反 FK 的半成品行)。
        owner_user_id = _coalesce_str(payload.get("owner_user_id"))
        project_id = _coalesce_str(payload.get("project_id"))
        if owner_user_id not in known_user_ids:
            rep.skip(f"job id={job_id} owner_user_id not resolvable from payload (skipped)")
            continue
        if project_id not in known_project_ids:
            rep.skip(f"job id={job_id} project_id not resolvable from payload (skipped)")
            continue

        raw_status = _coalesce_str(row.get("status")).lower()
        # 1.x: pending/running/succeeded/failed/cancelled/cancelling。
        # running / cancelling → failed(迁入时不可能在跑;契约明示 running 标 failed)。
        status_map = {
            "pending": "pending",
            "running": "failed",
            "cancelling": "failed",
            "succeeded": "success",
            "success": "success",
            "failed": "failed",
            "cancelled": "cancelled",
        }
        status = status_map.get(raw_status, "failed")
        if raw_status in ("running", "cancelling"):
            rep.warn(f"job id={job_id} was '{raw_status}' in 1.x → marked failed")

        kind = _coalesce_str(row.get("kind")) or "sql_query"
        timeout_seconds = payload.get("timeout_seconds")
        if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
            timeout_seconds = 900  # 1.x 默认 statement timeout(AS-IS §2.8)

        values: dict[str, Any] = {
            "id": job_id,
            "kind": kind,
            "status": status,
            "owner_user_id": owner_user_id,
            "project_id": project_id,
            "timeout_seconds": timeout_seconds,
            # audit_id NOT NULL,1.x 无对应;用 job_id 占位(同表唯一,满足非空 + 可追溯)。
            "audit_id": job_id,
            "error": _coalesce_str(row.get("error")) or None,
        }
        started = _parse_dt(row.get("started_at"))
        if started is not None:
            values["started_at"] = started
        finished = _parse_dt(row.get("finished_at"))
        if finished is not None:
            values["finished_at"] = finished
        conn.execute(insert(jobs).values(**values))
        rep.migrated += 1


def migrate_audit_logs(
    conn: Connection,
    rows: Sequence[Mapping[str, Any]],
    report: MigrationReport,
) -> None:
    """迁 SQLite audit_logs → audit_logs(无 FK,容错宽)。"""
    rep = report.table("audit_logs")
    for row in rows:
        ts = _parse_dt(row.get("ts"))
        method = _coalesce_str(row.get("method"))
        path = _coalesce_str(row.get("path"))
        # 2.0 action NOT NULL:1.x 用 method+path 描述操作,合成 action。
        action = f"{method} {path}".strip() or "legacy_audit"
        if len(action) > 64:
            action = action[:64]

        # 2.0 result NOT NULL(success/denied/error):1.x 是 HTTP status。
        status = row.get("status")
        result = "success"
        if isinstance(status, int):
            if status in (401, 403):
                result = "denied"
            elif status >= 400:
                result = "error"
        elif isinstance(status, str) and status.isdigit():
            code = int(status)
            if code in (401, 403):
                result = "denied"
            elif code >= 400:
                result = "error"

        values: dict[str, Any] = {
            "user_id": _coalesce_str(row.get("user_id")) or None,
            "project_id": _coalesce_str(row.get("project_id")) or None,
            "action": action,
            "resource_type": _coalesce_str(row.get("resource")) or None,
            "resource_id": _coalesce_str(row.get("resource_id")) or None,
            "result": result,
            "request_id": _coalesce_str(row.get("request_id")) or None,
        }
        if ts is not None:
            values["ts"] = ts
        # 1.x extra 是 JSON blob;放进 detail(脱敏由 audit 写入侧 / 查询侧负责,这里只搬运摘要)。
        extra = row.get("extra")
        if isinstance(extra, dict) and extra:
            values["detail"] = extra
        conn.execute(insert(audit_logs).values(**values))
        rep.migrated += 1


# ─────────────────────────────────────────────────────────────────────────────
# 编排
# ─────────────────────────────────────────────────────────────────────────────
def run_migration(
    *,
    source_dir: Path,
    engine: Engine,
    secret_store: SecretStore,
    v1_decryptor: V1FernetDecryptor,
) -> MigrationReport:
    """执行整库迁移。返回报告。失败时 report.incomplete=True(已写入部分保留)。

    ★ 不在一个大事务里跑全部:secret_store.store_secret 走自己的连接/事务,
      与 PG 行写入分开。每张表用独立事务,失败保留前面已提交的表(报告标 incomplete),
      契约 §5.4.2:迁移失败保留已写入部分但明确标记不完整。
    """
    report = MigrationReport()
    report.skipped_sources = list(_SKIPPED_SOURCES)

    users_rows = load_json_list(source_dir, "users.json")
    projects_rows = load_json_list(source_dir, "projects.json")
    datasources_rows = load_json_list(source_dir, "datasources.json")

    try:
        with engine.begin() as conn:
            user_ids = migrate_users(conn, users_rows, secret_store, v1_decryptor, report)
        with engine.begin() as conn:
            project_ids = migrate_projects(conn, projects_rows, user_ids, report)
        with engine.begin() as conn:
            migrate_datasources(conn, datasources_rows, project_ids, secret_store, report)

        with open_sqlite(source_dir) as sqlite_conn:
            if sqlite_conn is None:
                report.table("jobs").warn("data/dataops.db not found → jobs skipped")
                report.table("audit_logs").warn("data/dataops.db not found → audit_logs skipped")
            else:
                jobs_rows = read_sqlite_rows(sqlite_conn, "jobs")
                audit_rows = read_sqlite_rows(sqlite_conn, "audit_logs")
                with engine.begin() as conn:
                    migrate_jobs(conn, jobs_rows, user_ids, project_ids, report)
                with engine.begin() as conn:
                    migrate_audit_logs(conn, audit_rows, report)
    except Exception as exc:
        report.incomplete = True
        report.fatal_error = type(exc).__name__
        logger.error("migration aborted; partial data retained", error_type=type(exc).__name__)
    return report


def log_report(report: MigrationReport) -> None:
    """打印迁移报告(structlog,脱敏 processor 兜底;本就不含明文)。"""
    for name, rep in report.tables.items():
        logger.info(
            "table migrated",
            table=name,
            migrated=rep.migrated,
            skipped_rows=rep.skipped_rows,
            warnings=len(rep.warnings),
        )
        for w in rep.warnings:
            logger.warning("field warning", table=name, detail=w)
        for s in rep.skips:
            logger.warning("row skipped", table=name, detail=s)
    logger.info("secrets re-encrypted", count=report.secrets_created)
    for source_label, reason in report.skipped_sources:
        logger.info("source skipped (no 2.0.0 table)", source=source_label, reason=reason)
    if report.incomplete:
        logger.error(
            "MIGRATION INCOMPLETE — partial data retained; do NOT treat as success",
            fatal_error_type=report.fatal_error,
        )
    else:
        logger.info("migration complete")


def _build_secret_store(master_key_file: Path, engine: Engine) -> SecretStore:
    master_key = master_key_file.read_bytes().strip()
    bootstrap = _MasterKeyOnlyBootstrap(master_key=master_key)
    # allow_audit_failure=True:迁移期 store_secret 不触发 reveal 审计;
    # 但若 audit_logs 表写入受限也不应阻塞迁移(store_secret 不写审计,这里仅兜底)。
    return LocalFileSecretStore(engine, bootstrap)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="migrate_from_v1",
        description="Migrate DataOpsStudio 1.x data into 2.0 PostgreSQL.",
    )
    parser.add_argument(
        "--source", required=True, type=Path, help="1.x 实例根目录(含 config/ data/)"
    )
    parser.add_argument("--target", required=True, help="2.0 PG DSN(SQLAlchemy URL)")
    parser.add_argument(
        "--v1-secret-key",
        required=True,
        type=Path,
        help="1.x config/.dataops_secret.key 路径(解密旧 Fernet 字段)",
    )
    parser.add_argument(
        "--master-key-file",
        required=True,
        type=Path,
        help="2.0 新 master key 文件(.secret_master.key,重加密用)",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    configure_logging(args.log_level)

    source_dir: Path = args.source
    if not source_dir.is_dir():
        logger.error("source dir not found", source=str(source_dir))
        return 2

    try:
        v1_decryptor = V1FernetDecryptor.from_key_file(args.v1_secret_key)
    except V1SecretDecryptError:
        logger.error("failed to load v1 secret key", path=str(args.v1_secret_key))
        return 2

    engine = create_engine(args.target)
    try:
        secret_store = _build_secret_store(args.master_key_file, engine)
        report = run_migration(
            source_dir=source_dir,
            engine=engine,
            secret_store=secret_store,
            v1_decryptor=v1_decryptor,
        )
    finally:
        engine.dispose()

    log_report(report)
    # 迁移失败标 incomplete → 非零退出,防 2.0 启动把半成品当成功(设计稿 §5.4.2)。
    return 1 if report.incomplete else 0


if __name__ == "__main__":
    sys.exit(main())
