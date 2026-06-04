"""SQLAlchemy 2 Core 元数据 —— 10 张表(契约 §5、设计稿 §5.1)。

★ R4 红线 DB 层防御(secret_refs CHECK):
  kind 限定为 Application Secret 6 种,Bootstrap kind(master_key /
  pg_*_password / license)**永不可插入**。配合 SecretStore.store_secret
  Python 层防御 = 双重保险。

★ R6 红线 DB 层防御(result_sets):
  本表禁止任何 cursor* 字段。新增字段时若引入 cursor* 命名,Step 1.7
  ast-grep 会扫此文件 + tests/unit/test_models 也会失败。

★ secret_ref 字段全部 VARCHAR(64) **无 FK**(跨存储逻辑引用):
  hosted 用 KmsSecretStore 时 secret 不在 PG,加 FK 会让 KMS 实现违反约束。
  users.mfa_secret_ref / datasources.password_secret_ref / 等同理。

★ PG queue 关键索引(契约 §2.2.2 + FOR UPDATE SKIP LOCKED):
  ix_jobs_queue_pending —— 偏序索引,WHERE status='pending',覆盖待处理任务。
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

# 命名约定 —— Alembic autogenerate 友好,避免匿名约束跨版本漂移
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


# ★ 与 app.domain.secret.SecretKind 严格一致;DB CHECK 防 Bootstrap kind 越界(R4)
APPLICATION_SECRET_KINDS: tuple[str, ...] = (
    "datasource_password",
    "ai_api_key",
    "mfa_totp_seed",
    "oauth_token",
    "webhook_secret",
    "signed_url_secret",
)


# ─────────────────────────────────────────────────────────────────────────────
# users
# ─────────────────────────────────────────────────────────────────────────────
users = Table(
    "users",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("username", String(255), nullable=False, unique=True),
    # bcrypt hash(60 字符左右,留余量)
    Column("password_hash", String(255), nullable=False),
    # ★ NO FK on secret refs(跨存储,KMS 实现下不在 PG)
    Column("mfa_secret_ref", String(64), nullable=True),
    Column("role", String(32), nullable=False, server_default="viewer"),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
)


# ─────────────────────────────────────────────────────────────────────────────
# projects
# ─────────────────────────────────────────────────────────────────────────────
projects = Table(
    "projects",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("name", String(255), nullable=False),
    Column(
        "owner_user_id",
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("description", Text(), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)


# ─────────────────────────────────────────────────────────────────────────────
# project_members
# ─────────────────────────────────────────────────────────────────────────────
project_members = Table(
    "project_members",
    metadata,
    Column(
        "project_id",
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "user_id",
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("role", String(32), nullable=False, server_default="member"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("project_id", "user_id", name="uq_project_members_project_user"),
)


# ─────────────────────────────────────────────────────────────────────────────
# datasources
# ─────────────────────────────────────────────────────────────────────────────
datasources = Table(
    "datasources",
    metadata,
    Column("id", String(36), primary_key=True),
    Column(
        "project_id",
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("name", String(255), nullable=False),
    Column("db_type", String(32), nullable=False),  # mysql / oracle / dm / db2 / postgresql
    Column("host", String(255), nullable=False),
    Column("port", Integer(), nullable=False),
    Column("username", String(255), nullable=False),
    # 业务连接的库/schema/service —— 与 DatasourceConnInfo.database 对应。
    # ★ 列名用 database_name(避 SQL 保留字 `database`);
    #   domain 字段叫 database,应用层做名字映射。
    Column("database_name", String(128), nullable=True),
    # ★ NO FK(跨存储 secret 引用,见 SecretRef domain)
    Column("password_secret_ref", String(64), nullable=False),
    Column("environment", String(32), nullable=False, server_default="dev"),
    Column("environment_verified", Boolean(), nullable=False, server_default=text("false")),
    Column("capability_profile", JSONB(), nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("project_id", "name", name="uq_datasources_project_name"),
)


# ─────────────────────────────────────────────────────────────────────────────
# jobs —— PG queue 抢任务表(FOR UPDATE SKIP LOCKED,契约 §2.2.2)
# ─────────────────────────────────────────────────────────────────────────────
jobs = Table(
    "jobs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("kind", String(32), nullable=False),
    Column("status", String(32), nullable=False, server_default="pending"),
    Column(
        "owner_user_id",
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "project_id",
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "datasource_ids",
        ARRAY(String(36)),
        nullable=False,
        server_default=text("'{}'::varchar[]"),
    ),
    Column("priority", Integer(), nullable=False, server_default="0"),
    Column("timeout_seconds", Integer(), nullable=False),
    # resource_profile —— Step 0 补,与设计稿 §2.5 对齐
    Column(
        "resource_profile",
        JSONB(),
        nullable=False,
        server_default=text("'{}'::jsonb"),
    ),
    # result_ref:JSONB 序列化 ResultRef(backend + uri),完成时填
    Column("result_ref", JSONB(), nullable=True),
    Column("audit_id", String(36), nullable=False),
    Column("worker_id", String(64), nullable=True),
    Column("last_heartbeat", DateTime(timezone=True), nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("cancel_requested", Boolean(), nullable=False, server_default=text("false")),
    Column("cancel_reason", String(255), nullable=True),
    Column("error", Text(), nullable=True),
    Column("retry_count", Integer(), nullable=False, server_default="0"),
    Column("payload", JSONB(), nullable=False, server_default=text("'{}'::jsonb")),
    Column("parent_workflow_run_id", String(36), nullable=True),  # 自引用 workflow 父 run
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    # ★ 关键:status='pending' 偏序索引;FOR UPDATE SKIP LOCKED 走这条极小索引
    Index(
        "ix_jobs_queue_pending",
        "priority",
        "created_at",
        postgresql_where=text("status = 'pending'"),
    ),
    # worker 心跳监控:扫 running 状态找超时
    Index(
        "ix_jobs_worker_heartbeat",
        "worker_id",
        "last_heartbeat",
        postgresql_where=text("status = 'running'"),
    ),
    # owner / project 时间序常用查询
    Index("ix_jobs_owner_user_id", "owner_user_id", "created_at"),
    Index("ix_jobs_project_id", "project_id", "created_at"),
)


# ─────────────────────────────────────────────────────────────────────────────
# job_events —— job 事件流(高频写入,id 用 BIGSERIAL)
# ─────────────────────────────────────────────────────────────────────────────
job_events = Table(
    "job_events",
    metadata,
    Column("id", BigInteger(), primary_key=True, autoincrement=True),
    Column(
        "job_id",
        String(36),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("ts", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("event_type", String(64), nullable=False),
    Column("message", Text(), nullable=True),
    Column("detail", JSONB(), nullable=True),
    Index("ix_job_events_job_ts", "job_id", "ts"),
)


# ─────────────────────────────────────────────────────────────────────────────
# secret_refs —— Application Secret only(R4 红线 CHECK + Python 层双重防御)
# ─────────────────────────────────────────────────────────────────────────────
secret_refs = Table(
    "secret_refs",
    metadata,
    Column("ref", String(64), primary_key=True),
    Column("kind", String(32), nullable=False),
    Column("ciphertext", LargeBinary(), nullable=False),
    Column("created_by", String(36), nullable=True),  # user_id 跨存储,无 FK
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("last_accessed_at", DateTime(timezone=True), nullable=True),
    Column("rotation_required", Boolean(), nullable=False, server_default=text("false")),
    # ★ R4 DB 层防御:kind 必须在 Application Secret 6 种之一
    CheckConstraint(
        "kind IN ('datasource_password', 'ai_api_key', 'mfa_totp_seed', "
        "'oauth_token', 'webhook_secret', 'signed_url_secret')",
        name="kind_is_application_secret",
    ),
    Index("ix_secret_refs_kind", "kind"),
)


# ─────────────────────────────────────────────────────────────────────────────
# audit_logs —— 合规审计(高频写入,id BIGSERIAL)
# 通过 request_id 与运行日志关联(设计稿 §9.2.1 / §9.3)
# ─────────────────────────────────────────────────────────────────────────────
audit_logs = Table(
    "audit_logs",
    metadata,
    Column("id", BigInteger(), primary_key=True, autoincrement=True),
    Column("ts", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    # user_id / project_id 可空(系统操作 / 无项目上下文)
    Column("user_id", String(36), nullable=True),
    Column("project_id", String(36), nullable=True),
    Column("action", String(64), nullable=False),  # login / sql_execute / secret_reveal / ...
    Column("resource_type", String(64), nullable=True),
    Column("resource_id", String(64), nullable=True),
    Column("result", String(32), nullable=False),  # success / denied / error
    Column("request_id", String(64), nullable=True),  # ★ 关联运行日志
    Column("ip", String(45), nullable=True),  # IPv6 max 长
    Column("user_agent", Text(), nullable=True),
    Column("detail", JSONB(), nullable=True),  # 摘要,同样不存敏感值
    # 时序、用户、资源、关联、操作型查询
    Index("ix_audit_logs_ts", text("ts DESC")),
    Index("ix_audit_logs_user_id_ts", "user_id", text("ts DESC")),
    Index("ix_audit_logs_resource", "resource_type", "resource_id"),
    Index("ix_audit_logs_request_id", "request_id"),
    Index("ix_audit_logs_action_ts", "action", text("ts DESC")),
)


# ─────────────────────────────────────────────────────────────────────────────
# result_sets —— ★ R6 红线 DB 层:无 cursor 字段
# ─────────────────────────────────────────────────────────────────────────────
result_sets = Table(
    "result_sets",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("execution_id", String(36), nullable=False),
    # storage_ref:JSONB(backend + uri),指向 spool;不是 cursor 引用
    Column("storage_ref", JSONB(), nullable=False),
    Column("columns", JSONB(), nullable=False),
    Column("loaded_rows", Integer(), nullable=False, server_default="0"),
    Column("total_rows", Integer(), nullable=True),
    Column("state", String(32), nullable=False, server_default="streaming"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Index("ix_result_sets_execution_id", "execution_id"),
    CheckConstraint(
        "state IN ('streaming', 'complete', 'failed', 'closed')",
        name="state_is_valid",
    ),
)


# ─────────────────────────────────────────────────────────────────────────────
# license_state —— singleton(只一行 id=1)
# ─────────────────────────────────────────────────────────────────────────────
license_state = Table(
    "license_state",
    metadata,
    Column("id", Integer(), primary_key=True),
    Column("edition", String(32), nullable=True),
    Column("customer", String(255), nullable=True),
    Column("expires_at", DateTime(timezone=True), nullable=True),
    Column("features", JSONB(), nullable=False, server_default=text("'[]'::jsonb")),
    Column("signature_verified_at", DateTime(timezone=True), nullable=True),
    Column("grace_started_at", DateTime(timezone=True), nullable=True),
    Column("mode", String(32), nullable=False, server_default="trial"),
    Column("repair_reason", String(255), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint("id = 1", name="singleton"),
    CheckConstraint("mode IN ('trial', 'valid', 'in_grace', 'repair')", name="mode_is_valid"),
)


__all__ = [
    "APPLICATION_SECRET_KINDS",
    "audit_logs",
    "datasources",
    "job_events",
    "jobs",
    "license_state",
    "metadata",
    "project_members",
    "projects",
    "result_sets",
    "secret_refs",
    "users",
]
