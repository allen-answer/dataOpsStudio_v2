"""migrate_from_v1.py —— DataOpsStudio 1.x → 2.0 数据迁移(契约 §5 / 设计稿 §5.4)。

目标:把 1.x 的 config/*.json + data/dataops.db 迁进 2.0 统一 PostgreSQL。

用法:
    python -m tools.migrate_from_v1 \
        --source <v1_dir> \
        --target <pg_dsn> \
        --v1-secret-key <path/to/.dataops_secret.key> \
        --master-key-file <path/to/2.0/.secret_master.key> \
        [--global-datasource-project <name> [--global-datasource-owner <username>]]

边界(严守):
- 只迁当前 2.x 已建表的源数据(以 app/db/models.py 实测为准):
    users / projects(+project_members) / datasources / workflows /
    workflow_templates / jobs / audit_logs
- 1.x 全局数据源(project_id 为空)无法满足 2.0 NOT NULL FK:默认跳过 + 报告;
  带 --global-datasource-project 时新建/复用同名承接项目挂入(owner 从已迁移用户
  按 username 解析,默认 admin,解析不到报错退出)。见 docs/deployment/migrate-from-v1.md。
- 尚未建表的后续能力域(compare_tasks / scenario_templates /
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
from uuid import uuid4

import structlog
from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import Connection, Engine

from app.db.models import (
    audit_logs,
    datasources,
    jobs,
    project_members,
    projects,
    users,
    workflow_templates,
    workflows,
)
from app.domain.secret import SecretKind
from app.domain.workflow import WorkflowSpec
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

_WORKFLOW_NODE_KIND_MAP: dict[str, str] = {
    "compare": "compare_run",
    "lineage": "lineage_analyze",
    "excel_export": "export_excel",
}
_WORKFLOW_FORBIDDEN_NODE_KINDS: frozenset[str] = frozenset({"http"})
_WORKFLOW_UNMAPPABLE_NODE_KINDS: frozenset[str] = frozenset({"params"})
_WORKFLOW_DEFAULT_TIMEOUT_SECONDS = 900

# 2.0.0 骨架未建表的 1.x 源(设计稿 §5.4 含 2.1+ 目标表)。逐项列"跳过 + 原因"。
_SKIPPED_SOURCES: tuple[tuple[str, str], ...] = (
    ("config/tasks.json → compare_tasks", "2.0.0 骨架未建 compare_tasks 表(2.2 Compare)"),
    (
        "config/scenarios/*.yml → scenario_templates",
        "2.0.0 骨架未建 scenario_templates 表(2.6 Scenario)",
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
    failed_rows: int = 0
    skipped_rows: int = 0
    warnings: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    skips: list[str] = field(default_factory=list)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def fail(self, msg: str) -> None:
        self.failed_rows += 1
        self.failures.append(msg)

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
# 迁移选项(CLI 旗标解析后的载体;均可选,默认 = 现行为)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class MigrationOptions:
    """run_migration 的可选行为开关。

    global_datasource_project:为 1.x 全局数据源(project_id 为空)新建/复用承接项目名。
        None = 现行为(全局数据源跳过 + 报告)。
    global_datasource_owner:承接项目的 owner username(从已迁移用户按 username 解析)。
    """

    global_datasource_project: str | None = None
    global_datasource_owner: str = "admin"


class MigrationConfigError(RuntimeError):
    """迁移旗标语义错误(如承接项目 owner 解析不到已迁移用户)。"""


class WorkflowMigrationError(RuntimeError):
    """Workflow 定义含禁止/不可映射/无效节点,必须人工改写后重跑迁移。"""


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
) -> tuple[set[str], dict[str, str]]:
    """迁 config/users.json → users。

    返回 (成功迁入的 user id 集合, username → user_id 映射)。
    id 集合供 FK 校验;username 映射供全局数据源承接项目按 owner username 解析。
    """
    rep = report.table("users")
    migrated_ids: set[str] = set()
    username_to_id: dict[str, str] = {}
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
        username_to_id[username] = user_id
        rep.migrated += 1
    return migrated_ids, username_to_id


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
    global_project_id: str | None = None,
) -> int:
    """迁 config/datasources.json → datasources。明文 password → SecretStore → secret_ref。

    global_project_id:不为 None 时,project_id 为空的 1.x 全局数据源挂入该承接项目;
        为 None 时维持现行为(全局数据源跳过 + 报告)。
    返回挂入承接项目的全局数据源条数(供 synthetic project 报告 / log)。
    """
    rep = report.table("datasources")
    global_attached = 0
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
        raw_project_id = _coalesce_str(row.get("project_id"))
        is_global = not raw_project_id
        if is_global and global_project_id is not None:
            # 带 --global-datasource-project:全局数据源挂入承接项目。
            project_id = global_project_id
            global_attached += 1
        elif raw_project_id and raw_project_id in known_project_ids:
            project_id = raw_project_id
        else:
            # 全局且未指定承接项目 / 项目缺失或未迁 → 跳过(维持现行为)。
            rep.skip(
                f"datasource id={ds_id} project_id not migrated/missing "
                f"(project={raw_project_id or '<global>'})"
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
    return global_attached


def _workflow_created_by(
    raw_owner: object,
    known_user_ids: set[str],
    username_to_id: Mapping[str, str],
) -> str | None:
    owner = _coalesce_str(raw_owner)
    if owner in known_user_ids:
        return owner
    return username_to_id.get(owner)


def _workflow_timeout_seconds(config: Mapping[str, Any]) -> int:
    raw_timeout = config.get("timeout_seconds")
    if isinstance(raw_timeout, int) and raw_timeout > 0:
        return raw_timeout
    return _WORKFLOW_DEFAULT_TIMEOUT_SECONDS


def _workflow_node_payload(
    *,
    legacy_kind: str,
    legacy_name: str,
    config: Mapping[str, Any],
    report: TableReport,
    workflow_label: str,
    node_id: str,
) -> dict[str, Any]:
    payload = dict(config)
    payload["legacy_node_type"] = legacy_kind
    if legacy_name:
        payload["legacy_node_name"] = legacy_name
    if config:
        report.warn(
            f"{workflow_label} node id={node_id} legacy config preserved in payload "
            f"(keys={','.join(sorted(str(key) for key in config))})"
        )
    return payload


def _workflow_spec_from_v1(
    row: Mapping[str, Any],
    *,
    table: str,
    report: TableReport,
) -> dict[str, Any] | None:
    workflow_id = _coalesce_str(row.get("id"))
    workflow_name = _coalesce_str(row.get("name"))
    workflow_label = f"{table} id={workflow_id or '<none>'} name={workflow_name or '<none>'}"
    raw_nodes = row.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        report.fail(f"validation_failed: {workflow_label} has no nodes")
        return None

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    blocked = False
    for raw_node in raw_nodes:
        if not isinstance(raw_node, Mapping):
            report.fail(f"validation_failed: {workflow_label} contains non-object node")
            blocked = True
            continue
        node_id = _coalesce_str(raw_node.get("id"))
        legacy_kind = _coalesce_str(raw_node.get("type")) or _coalesce_str(raw_node.get("kind"))
        if not node_id:
            report.fail(f"validation_failed: {workflow_label} contains node without id")
            blocked = True
            continue
        if legacy_kind in _WORKFLOW_FORBIDDEN_NODE_KINDS:
            report.fail(f"forbidden: {workflow_label} node id={node_id} type={legacy_kind}")
            blocked = True
            continue
        if legacy_kind in _WORKFLOW_UNMAPPABLE_NODE_KINDS:
            report.fail(f"unmappable: {workflow_label} node id={node_id} type={legacy_kind}")
            blocked = True
            continue
        job_kind = _WORKFLOW_NODE_KIND_MAP.get(legacy_kind)
        if job_kind is None:
            report.fail(
                f"unmappable: {workflow_label} node id={node_id} type={legacy_kind or '<none>'}"
            )
            blocked = True
            continue

        raw_config = raw_node.get("config")
        config: Mapping[str, Any] = dict(raw_config) if isinstance(raw_config, Mapping) else {}
        when = _coalesce_str(raw_node.get("when")) or None
        legacy_name = _coalesce_str(raw_node.get("name"))
        nodes.append(
            {
                "id": node_id,
                "job_kind": job_kind,
                "payload": _workflow_node_payload(
                    legacy_kind=legacy_kind,
                    legacy_name=legacy_name,
                    config=config,
                    report=report,
                    workflow_label=workflow_label,
                    node_id=node_id,
                ),
                "timeout_seconds": _workflow_timeout_seconds(config),
                "when": when,
            }
        )

        depends_on = raw_node.get("depends_on")
        if isinstance(depends_on, list):
            for source in depends_on:
                if isinstance(source, str) and source:
                    edges.append({"source": source, "target": node_id})
        elif depends_on is not None:
            report.fail(
                f"validation_failed: {workflow_label} node id={node_id} depends_on is not a list"
            )
            blocked = True

    if blocked:
        return None

    schedule = None
    cron = _coalesce_str(row.get("schedule_cron"))
    if cron:
        schedule = {
            "cron": cron,
            "enabled": _coalesce_str(row.get("status")).lower() == "active",
        }
    candidate: dict[str, Any] = {"nodes": nodes, "edges": edges, "schedule": schedule}
    try:
        return WorkflowSpec.model_validate(candidate).model_dump(mode="json")
    except Exception as exc:
        report.fail(f"validation_failed: {workflow_label}: {exc}")
        return None


def migrate_workflows(
    conn: Connection,
    rows: Sequence[Mapping[str, Any]],
    known_project_ids: set[str],
    known_user_ids: set[str],
    username_to_id: Mapping[str, str],
    report: MigrationReport,
) -> None:
    """迁 config/workflows.json → workflows;发现禁止/不可映射/无效节点则整表不写。"""
    rep = report.table("workflows")
    inserts: list[dict[str, Any]] = []
    for row in rows:
        workflow_id = _coalesce_str(row.get("id"))
        name = _coalesce_str(row.get("name"))
        project_id = _coalesce_str(row.get("project_id"))
        if not (workflow_id and name):
            rep.skip(f"workflow missing id/name (id={workflow_id or '<none>'})")
            continue
        if project_id not in known_project_ids:
            rep.skip(
                f"workflow id={workflow_id} project_id not migrated/missing "
                f"(project={project_id or '<none>'})"
            )
            continue
        dag_jsonb = _workflow_spec_from_v1(row, table="workflows", report=rep)
        if dag_jsonb is None:
            continue
        schedule = dag_jsonb.get("schedule")
        created = _parse_dt(row.get("created_at"))
        values: dict[str, Any] = {
            "id": workflow_id,
            "project_id": project_id,
            "name": name,
            "dag_jsonb": dag_jsonb,
            "schedule_cron": schedule.get("cron") if isinstance(schedule, dict) else None,
            "schedule_enabled": (
                bool(schedule.get("enabled")) if isinstance(schedule, dict) else False
            ),
            "enabled": _coalesce_str(row.get("status")).lower() != "archived",
            "created_by": _workflow_created_by(row.get("owner"), known_user_ids, username_to_id),
        }
        if created is not None:
            values["created_at"] = created
            values["updated_at"] = created
        inserts.append(values)

    if rep.failed_rows:
        raise WorkflowMigrationError("workflows contain forbidden/unmappable/invalid nodes")
    for values in inserts:
        conn.execute(insert(workflows).values(**values))
        rep.migrated += 1


def migrate_workflow_templates(
    conn: Connection,
    rows: Sequence[Mapping[str, Any]],
    known_user_ids: set[str],
    username_to_id: Mapping[str, str],
    report: MigrationReport,
) -> None:
    """迁 config/workflow_templates.json → workflow_templates;模板无项目归属。"""
    rep = report.table("workflow_templates")
    inserts: list[dict[str, Any]] = []
    for row in rows:
        template_id = _coalesce_str(row.get("id"))
        name = _coalesce_str(row.get("name"))
        workflow = row.get("workflow")
        if not (template_id and name):
            rep.skip(f"workflow_template missing id/name (id={template_id or '<none>'})")
            continue
        if not isinstance(workflow, Mapping):
            rep.fail(
                f"validation_failed: workflow_templates id={template_id} has no workflow object"
            )
            continue
        workflow_row = dict(workflow)
        workflow_row.setdefault("id", template_id)
        workflow_row.setdefault("name", name)
        dag_jsonb = _workflow_spec_from_v1(workflow_row, table="workflow_templates", report=rep)
        if dag_jsonb is None:
            continue
        created = _parse_dt(row.get("created_at")) or _parse_dt(workflow.get("created_at"))
        values: dict[str, Any] = {
            "id": template_id,
            "name": name,
            "description": _coalesce_str(row.get("description")) or None,
            "dag_jsonb": dag_jsonb,
            "created_by": _workflow_created_by(
                workflow.get("owner"), known_user_ids, username_to_id
            ),
        }
        if created is not None:
            values["created_at"] = created
            values["updated_at"] = created
        inserts.append(values)

    if rep.failed_rows:
        raise WorkflowMigrationError(
            "workflow_templates contain forbidden/unmappable/invalid nodes"
        )
    for values in inserts:
        conn.execute(insert(workflow_templates).values(**values))
        rep.migrated += 1


def create_global_datasource_project(
    conn: Connection,
    *,
    project_name: str,
    owner_user_id: str,
    report: MigrationReport,
) -> str:
    """新建承接 1.x 全局数据源的 synthetic 项目 + owner membership,返回 project id。

    若已存在同名已迁项目则复用(幂等承接,不重复建)。仅在确有全局数据源时被调用。
    计入 projects 报告(migrated +1)与 project_members(owner)。
    """
    existing = conn.execute(
        select(projects.c.id).where(projects.c.name == project_name)
    ).scalar_one_or_none()
    if existing is not None:
        return str(existing)

    # R10:别 f-string 拼 uuid;直接用 str(uuid4())。
    project_id = str(uuid4())
    conn.execute(
        insert(projects).values(
            id=project_id,
            name=project_name,
            owner_user_id=owner_user_id,
            description="migrated: 1.x global datasources",
        )
    )
    report.table("projects").migrated += 1
    conn.execute(
        insert(project_members).values(project_id=project_id, user_id=owner_user_id, role="owner")
    )
    report.table("project_members").migrated += 1
    return project_id


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
def _has_global_datasource(rows: Sequence[Mapping[str, Any]]) -> bool:
    """是否存在 project_id 为空的 1.x 全局数据源(决定是否新建承接项目)。"""
    return any(not _coalesce_str(row.get("project_id")) for row in rows)


def _resolve_global_owner(options: MigrationOptions, username_to_id: dict[str, str]) -> str:
    """从已迁移用户按 username 解析承接项目 owner;解析不到 → MigrationConfigError(非静默)。"""
    owner_id = username_to_id.get(options.global_datasource_owner)
    if owner_id is None:
        available = ", ".join(sorted(username_to_id)) or "<none migrated>"
        raise MigrationConfigError(
            f"--global-datasource-owner {options.global_datasource_owner!r} "
            f"did not resolve to a migrated user; available usernames: {available}"
        )
    return owner_id


def run_migration(
    *,
    source_dir: Path,
    engine: Engine,
    secret_store: SecretStore,
    v1_decryptor: V1FernetDecryptor,
    options: MigrationOptions | None = None,
) -> MigrationReport:
    """执行整库迁移。返回报告。失败时 report.incomplete=True(已写入部分保留)。

    ★ 不在一个大事务里跑全部:secret_store.store_secret 走自己的连接/事务,
      与 PG 行写入分开。每张表用独立事务,失败保留前面已提交的表(报告标 incomplete),
      契约 §5.4.2:迁移失败保留已写入部分但明确标记不完整。

    options.global_datasource_project 不为 None 且确有全局数据源时,新建承接项目挂入;
    owner 解析失败抛 MigrationConfigError(不吞,向上传递,主流程明确报错退出)。
    """
    options = options or MigrationOptions()
    report = MigrationReport()
    report.skipped_sources = list(_SKIPPED_SOURCES)

    users_rows = load_json_list(source_dir, "users.json")
    projects_rows = load_json_list(source_dir, "projects.json")
    datasources_rows = load_json_list(source_dir, "datasources.json")
    workflows_rows = load_json_list(source_dir, "workflows.json")
    workflow_templates_rows = load_json_list(source_dir, "workflow_templates.json")

    try:
        with engine.begin() as conn:
            user_ids, username_to_id = migrate_users(
                conn, users_rows, secret_store, v1_decryptor, report
            )
        with engine.begin() as conn:
            project_ids = migrate_projects(conn, projects_rows, user_ids, report)

        # 全局数据源承接项目:仅当带旗标 且 确有全局数据源时才建(没有就不建)。
        global_project_id: str | None = None
        if options.global_datasource_project is not None and _has_global_datasource(
            datasources_rows
        ):
            owner_id = _resolve_global_owner(options, username_to_id)
            with engine.begin() as conn:
                global_project_id = create_global_datasource_project(
                    conn,
                    project_name=options.global_datasource_project,
                    owner_user_id=owner_id,
                    report=report,
                )

        with engine.begin() as conn:
            attached = migrate_datasources(
                conn,
                datasources_rows,
                project_ids,
                secret_store,
                report,
                global_project_id=global_project_id,
            )
        with engine.begin() as conn:
            migrate_workflows(
                conn,
                workflows_rows,
                project_ids,
                user_ids,
                username_to_id,
                report,
            )
        with engine.begin() as conn:
            migrate_workflow_templates(
                conn,
                workflow_templates_rows,
                user_ids,
                username_to_id,
                report,
            )
        if global_project_id is not None:
            logger.info(
                "synthetic project created for 1.x global datasources",
                project=options.global_datasource_project,
                owner=options.global_datasource_owner,
                attached_datasources=attached,
            )

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
    except MigrationConfigError:
        # 旗标语义错误(如 owner 解析失败):非静默 → 向上传递,由 main 明确报错退出。
        raise
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
            failed_rows=rep.failed_rows,
            skipped_rows=rep.skipped_rows,
            warnings=len(rep.warnings),
        )
        for w in rep.warnings:
            logger.warning("field warning", table=name, detail=w)
        for f in rep.failures:
            logger.error("row failed", table=name, detail=f)
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
    parser.add_argument(
        "--global-datasource-project",
        default=None,
        help=(
            "为 1.x 全局数据源(project_id 为空)新建/复用同名承接项目并挂入。"
            "不传 = 全局数据源跳过 + 报告(向后兼容)。仅在确有全局数据源时才建项目。"
        ),
    )
    parser.add_argument(
        "--global-datasource-owner",
        default="admin",
        help=(
            "承接项目 owner 的 username,从已迁移用户按 username 解析(默认 admin)。"
            "解析不到则报错退出(指明可用 username)。"
        ),
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

    options = MigrationOptions(
        global_datasource_project=args.global_datasource_project,
        global_datasource_owner=args.global_datasource_owner,
    )

    engine = create_engine(args.target)
    try:
        secret_store = _build_secret_store(args.master_key_file, engine)
        try:
            report = run_migration(
                source_dir=source_dir,
                engine=engine,
                secret_store=secret_store,
                v1_decryptor=v1_decryptor,
                options=options,
            )
        except MigrationConfigError as exc:
            # 旗标语义错误:非静默报错退出(已写入部分保留,但配置错不能当成功)。
            logger.error("migration flag configuration error", detail=str(exc))
            return 2
    finally:
        engine.dispose()

    log_report(report)
    # 迁移失败标 incomplete → 非零退出,防 2.0 启动把半成品当成功(设计稿 §5.4.2)。
    return 1 if report.incomplete else 0


if __name__ == "__main__":
    sys.exit(main())
