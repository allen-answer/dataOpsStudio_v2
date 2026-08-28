"""SQLAlchemy 2 Core 元数据(契约 §5、设计稿 §5.1)。

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
    Numeric,
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
    "smtp_password",
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
    Column("mfa_pending_secret_ref", String(64), nullable=True),
    Column("role", String(32), nullable=False, server_default="viewer"),
    Column("tokens_revoked_after", DateTime(timezone=True), nullable=True),
    Column("last_used_totp_counter", BigInteger(), nullable=True),
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
# mfa_recovery_codes —— 一次性恢复码 bcrypt hash
# ─────────────────────────────────────────────────────────────────────────────
mfa_recovery_codes = Table(
    "mfa_recovery_codes",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "user_id",
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("code_hash", String(255), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("used_at", DateTime(timezone=True), nullable=True),
    Index("ix_mfa_recovery_codes_user_id", "user_id"),
    Index("ix_mfa_recovery_codes_unused", "user_id", "used_at"),
)


# ─────────────────────────────────────────────────────────────────────────────
# revoked_tokens —— JWT jti denylist + 过期惰性清理
# ─────────────────────────────────────────────────────────────────────────────
revoked_tokens = Table(
    "revoked_tokens",
    metadata,
    Column("jti", String(64), primary_key=True),
    Column(
        "user_id",
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("revoked_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Index("ix_revoked_tokens_user_id", "user_id"),
    Index("ix_revoked_tokens_expires_at", "expires_at"),
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
    Column(
        "operation_policy",
        JSONB(),
        nullable=False,
        server_default=text(
            """'{
            "allow_select": true,
            "allow_explain": false,
            "allow_dm_explain": false,
            "allow_oracle_plan_table": false,
            "allow_schema_import": false,
            "allow_schema_save": false,
            "allow_scenario_write": false,
            "allow_record_task": false
            }'::jsonb"""
        ),
    ),
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
    Column(
        "available_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
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
    Column("error_code", String(64), nullable=True),
    Column("retry_count", Integer(), nullable=False, server_default="0"),
    Column("payload", JSONB(), nullable=False, server_default=text("'{}'::jsonb")),
    Column("parent_workflow_run_id", String(36), nullable=True),  # 自引用 workflow 父 run
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    # ★ 关键:status='pending' 偏序索引;FOR UPDATE SKIP LOCKED 走这条极小索引
    Index(
        "ix_jobs_queue_pending",
        "available_at",
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
    # workflow 子 job 幂等兜底:同一 run 内同一节点只允许一个子 job
    # (stale worker 苏醒后的重复 enqueue 撞唯一索引被跳过,防节点双跑);
    # 前导列同时充当 list_jobs_by_parent 的邻接索引
    Index(
        "uq_jobs_workflow_node_per_run",
        "parent_workflow_run_id",
        text("(payload->>'workflow_node_id')"),
        unique=True,
        postgresql_where=text("parent_workflow_run_id IS NOT NULL"),
    ),
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
# sql_consoles —— SQL Workspace 2.1 多 Console
# ─────────────────────────────────────────────────────────────────────────────
sql_consoles = Table(
    "sql_consoles",
    metadata,
    Column("id", String(36), primary_key=True),
    Column(
        "owner_user_id",
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "datasource_id",
        String(36),
        ForeignKey("datasources.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("name", String(128), nullable=False),
    Column("sql", Text(), nullable=False, server_default=""),
    Column("pinned", Boolean(), nullable=False, server_default=text("false")),
    Column("session_epoch", BigInteger(), nullable=False, server_default=text("0")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Index("ix_sql_consoles_owner_updated", "owner_user_id", "updated_at"),
)


# ─────────────────────────────────────────────────────────────────────────────
# console_sessions —— Session Broker 会话恢复/审计锚点
# ───────────────────────────────────────────────────────────────────────────
_ACTIVE_CONSOLE_SESSION_STATES = "'connecting', 'idle', 'executing', 'cancelling', 'closing'"

console_sessions = Table(
    "console_sessions",
    metadata,
    Column("id", String(36), primary_key=True),
    Column(
        "console_id",
        String(36),
        ForeignKey("sql_consoles.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "datasource_id",
        String(36),
        ForeignKey("datasources.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "owner_user_id",
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("epoch", BigInteger(), nullable=False),
    Column("state", Text(), nullable=False),
    Column("broker_boot_id", String(36), nullable=False),
    Column("db_session_marker", String(64), nullable=True),
    Column("server_cancel", String(16), nullable=False, server_default="unknown"),
    Column("autocommit", Boolean(), nullable=False, server_default=text("true")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column(
        "last_activity_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Column("closed_at", DateTime(timezone=True), nullable=True),
    Column("close_reason", String(64), nullable=True),
    Column("error_code", String(64), nullable=True),
    CheckConstraint(
        "state IN ('connecting', 'idle', 'executing', 'cancelling', 'closing', "
        "'closed', 'session_lost', 'connect_failed')",
        name="state_is_supported",
    ),
    Index(
        "uq_console_active_session",
        "console_id",
        unique=True,
        postgresql_where=text(f"state IN ({_ACTIVE_CONSOLE_SESSION_STATES})"),
    ),
    Index(
        "ix_console_sessions_boot",
        "broker_boot_id",
        postgresql_where=text(f"state IN ({_ACTIVE_CONSOLE_SESSION_STATES})"),
    ),
)


# ───────────────────────────────────────────────────────────────────────────
# console_statements —— 会话内逐语句状态与幂等回执
# ────────────────────────────────────────────────────────────────────────────
console_statements = Table(
    "console_statements",
    metadata,
    Column("id", String(36), primary_key=True),
    Column(
        "session_id",
        String(36),
        ForeignKey("console_sessions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    # 冗余快照:历史查询不需要 join console_sessions。
    Column("console_id", String(36), nullable=False),
    Column("datasource_id", String(36), nullable=False),
    Column("owner_user_id", String(36), nullable=False),
    Column("epoch", BigInteger(), nullable=False),
    Column("seq", Integer(), nullable=False),
    Column("client_request_id", String(64), nullable=False),
    Column("sql_text", Text(), nullable=False),
    # Canonical form is ``sha256:`` + 64 hex characters.
    Column("sql_hash", String(71), nullable=False),
    Column("sql_len", Integer(), nullable=False),
    Column("statement_kind", String(16), nullable=False),
    Column("is_write", Boolean(), nullable=False),
    Column("state", Text(), nullable=False),
    Column("cancel_requested", Boolean(), nullable=False, server_default=text("false")),
    Column(
        "result_set_id",
        String(36),
        ForeignKey("result_sets.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("rows_affected", BigInteger(), nullable=True),
    Column("error_code", String(64), nullable=True),
    Column("error_summary", Text(), nullable=True),
    Column("timeout_seconds", Integer(), nullable=False),
    Column("script_id", String(36), nullable=True),
    Column("script_seq", Integer(), nullable=True),
    Column("resolved_by", String(36), nullable=True),
    Column("resolved_at", DateTime(timezone=True), nullable=True),
    Column("resolution", Text(), nullable=True),
    Column("submitted_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "state IN ('accepted', 'executing', 'streaming', 'succeeded', 'failed', "
        "'cancelled', 'timeout', 'outcome_unknown', 'skipped')",
        name="state_is_supported",
    ),
    UniqueConstraint(
        "session_id",
        "client_request_id",
        name="uq_console_statements_session_request",
    ),
)


# ────────────────────────────────────────────────────────────────────────────
# console_statement_events —— 语句事件流(取消阶梯逐级留痕)
# ───────────────────────────────────────────────────────────────────────────
console_statement_events = Table(
    "console_statement_events",
    metadata,
    Column("id", BigInteger(), primary_key=True, autoincrement=True),
    Column(
        "statement_id",
        String(36),
        ForeignKey("console_statements.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("ts", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("event_type", String(64), nullable=False),
    Column("message", Text(), nullable=True),
    Column("detail", JSONB(), nullable=True),
    Index("ix_console_statement_events_statement_ts", "statement_id", "ts"),
)


# ─────────────────────────────────────────────────────────────────────────────
# sql_templates —— SQL Workspace 2.1 模板(admin 写,所有人读)
# ─────────────────────────────────────────────────────────────────────────────
sql_templates = Table(
    "sql_templates",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("name", String(128), nullable=False),
    Column("description", Text(), nullable=True),
    Column("sql_text", Text(), nullable=False),
    Column("variables", JSONB(), nullable=False, server_default=text("'[]'::jsonb")),
    Column("category", String(64), nullable=False, server_default="general"),
    Column(
        "project_id",
        String(36),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column(
        "created_by",
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Index("ix_sql_templates_category_name", "category", "name"),
    Index("ix_sql_templates_project_id", "project_id"),
)


# ─────────────────────────────────────────────────────────────────────────────
# export_download_tokens —— SQL Workspace 导出一次性下载令牌(只存 token hash)
# ─────────────────────────────────────────────────────────────────────────────
export_download_tokens = Table(
    "export_download_tokens",
    metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("job_id", String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
    Column(
        "owner_user_id",
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("format", String(16), nullable=False),
    Column("filename", String(255), nullable=False),
    Column("content_type", String(128), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("consumed_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint(
        "format IN ('csv', 'excel', 'json', 'sql')",
        name="format_is_supported",
    ),
    Index("ix_export_download_tokens_owner_created", "owner_user_id", "created_at"),
    Index("ix_export_download_tokens_expires_at", "expires_at"),
)


# ─────────────────────────────────────────────────────────────────────────────
# metadata_caches —— SQL Workspace metadata browser cache(2.1 W2)
# ─────────────────────────────────────────────────────────────────────────────
metadata_caches = Table(
    "metadata_caches",
    metadata,
    Column("id", String(36), primary_key=True),
    Column(
        "datasource_id",
        String(36),
        ForeignKey("datasources.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("cache_level", String(32), nullable=False),  # schemas / tables / columns / indexes
    Column("schema_name", String(128), nullable=False, server_default=""),
    Column("table_name", String(128), nullable=False, server_default=""),
    Column("payload", JSONB(), nullable=False, server_default=text("'[]'::jsonb")),
    Column("refreshed_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint(
        "datasource_id",
        "cache_level",
        "schema_name",
        "table_name",
        name="uq_metadata_caches_key",
    ),
    Index("ix_metadata_caches_datasource_level", "datasource_id", "cache_level"),
    Index("ix_metadata_caches_expires_at", "expires_at"),
    CheckConstraint(
        "cache_level IN ('schemas', 'tables', 'columns', 'indexes')",
        name="cache_level_is_supported",
    ),
)


# ─────────────────────────────────────────────────────────────────────────────
# compare_tasks —— Compare 2.2 持久化任务定义
# ─────────────────────────────────────────────────────────────────────────────
compare_tasks = Table(
    "compare_tasks",
    metadata,
    Column("id", String(36), primary_key=True),
    Column(
        "project_id",
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("name", String(128), nullable=False),
    # 文件源对比(C-1):file kind 的一侧无 datasource,故 source_id/target_id 可空。
    Column(
        "source_id",
        String(36),
        ForeignKey("datasources.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column(
        "target_id",
        String(36),
        ForeignKey("datasources.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column("source_ref", JSONB(), nullable=False),
    Column("target_ref", JSONB(), nullable=False),
    Column("columns", JSONB(), nullable=False, server_default=text("'[]'::jsonb")),
    Column("compare_rules", JSONB(), nullable=False, server_default=text("'{}'::jsonb")),
    Column("run_limits", JSONB(), nullable=False, server_default=text("'{}'::jsonb")),
    Column(
        "created_by",
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("project_id", "name", name="uq_compare_tasks_project_name"),
    Index("ix_compare_tasks_project_updated", "project_id", "updated_at"),
    Index("ix_compare_tasks_source_id", "source_id"),
    Index("ix_compare_tasks_target_id", "target_id"),
)


# ─────────────────────────────────────────────────────────────────────────────
# run_index —— Compare/后续能力域统一结果反向索引
# ─────────────────────────────────────────────────────────────────────────────
run_index = Table(
    "run_index",
    metadata,
    Column("run_id", String(36), primary_key=True),
    Column("kind", String(32), nullable=False),
    Column("job_id", String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
    Column(
        "owner_user_id",
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "project_id",
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "task_id",
        String(36),
        ForeignKey("compare_tasks.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("status", String(32), nullable=False, server_default="pending"),
    Column("result_path", Text(), nullable=True),
    Column("bucket_spools", JSONB(), nullable=False, server_default=text("'{}'::jsonb")),
    Column("bucket_counts", JSONB(), nullable=False, server_default=text("'{}'::jsonb")),
    Column("progress", JSONB(), nullable=False, server_default=text("'{}'::jsonb")),
    Column("diff_profile", JSONB(), nullable=False, server_default=text("'{}'::jsonb")),
    Column("sample_result", JSONB(), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "kind IN ('compare_run')",
        name="kind_is_supported",
    ),
    CheckConstraint(
        "status IN ('pending', 'running', 'success', 'failed', 'cancelled', 'timeout')",
        name="status_is_valid",
    ),
    UniqueConstraint("job_id", name="uq_run_index_job_id"),
    Index("ix_run_index_owner_project_created", "owner_user_id", "project_id", "created_at"),
    Index("ix_run_index_task_created", "task_id", "created_at"),
)


# ─────────────────────────────────────────────────────────────────────────────
# lineage_runs / lineage_edges / lineage_column_edges —— Lineage 2.3 edge store
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# uploads —— 用户上传文件登记(血缘批量 ZIP / 对比文件源;文件本体在
# result_store uploads/ 目录,由 upload_ttl_hours GC;行随文件语义过期)
# ─────────────────────────────────────────────────────────────────────────────
uploads = Table(
    "uploads",
    metadata,
    Column("id", String(36), primary_key=True),
    Column(
        "project_id",
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "owner_user_id",
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("purpose", String(32), nullable=False),
    Column("filename", String(255), nullable=False),
    Column("content_type", String(128), nullable=False),
    Column("bytes", BigInteger(), nullable=False),
    Column("storage_uri", Text(), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint(
        "purpose IN ('lineage_batch', 'compare_source')",
        name="purpose_is_supported",
    ),
    Index("ix_uploads_project_created", "project_id", "created_at"),
)


lineage_runs = Table(
    "lineage_runs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column(
        "project_id",
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "datasource_id",
        String(36),
        ForeignKey("datasources.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("dialect", String(32), nullable=False),
    Column("source_ref", Text(), nullable=False),
    Column("sql_hash", String(64), nullable=False),
    # 边抽屉"这条边为什么存在"的 SQL 原文;0018 前的旧 run 为 NULL(前端显示未存档)
    Column("sql_text", Text(), nullable=True),
    Column("parser_version", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("parse_summary", JSONB(), nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    # 0030:refresh 重解析不再删除旧 run,改标"已被取代"——血缘解析历史永久留痕。
    # superseded_at IS NULL 即"生效中";生效行才参与缓存命中与血缘图遍历。
    Column("superseded_at", DateTime(timezone=True), nullable=True),
    Column(
        "superseded_by",
        String(36),
        ForeignKey("lineage_runs.id", ondelete="SET NULL"),
        nullable=True,
    ),
    CheckConstraint(
        "status IN ('success', 'failed')",
        name="status_is_valid",
    ),
    # 0030:唯一性只约束生效行(历史行同 cache key 可有多条)。
    Index(
        "uq_lineage_runs_cache_key_active",
        "project_id",
        "datasource_id",
        "dialect",
        "source_ref",
        "sql_hash",
        "parser_version",
        unique=True,
        postgresql_where=text("superseded_at IS NULL"),
    ),
    Index("ix_lineage_runs_project_hash", "project_id", "sql_hash"),
    Index("ix_lineage_runs_datasource_created", "datasource_id", "created_at"),
    Index("ix_lineage_runs_project_created", "project_id", "created_at"),
)


lineage_edges = Table(
    "lineage_edges",
    metadata,
    Column("id", String(36), primary_key=True),
    Column(
        "run_id",
        String(36),
        ForeignKey("lineage_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "project_id",
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("source_table", String(512), nullable=False),
    Column("target_table", String(512), nullable=False),
    Column("edge_kind", String(32), nullable=False),
    Column("inferred", Boolean(), nullable=False, server_default=text("false")),
    Column("inference_status", String(32), nullable=False, server_default="confirmed"),
    Column("confidence", Numeric(5, 4), nullable=False, server_default="1.0"),
    Column("sql_hash", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint(
        "edge_kind IN ('table')",
        name="edge_kind_is_supported",
    ),
    CheckConstraint(
        "inference_status IN ('confirmed', 'inferred', 'rejected')",
        name="inference_status_is_valid",
    ),
    Index("ix_lineage_edges_source_table", "project_id", "source_table"),
    Index("ix_lineage_edges_target_table", "project_id", "target_table"),
    Index("ix_lineage_edges_run_id", "run_id"),
)


lineage_column_edges = Table(
    "lineage_column_edges",
    metadata,
    Column("id", String(36), primary_key=True),
    Column(
        "run_id",
        String(36),
        ForeignKey("lineage_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "project_id",
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("source_table", String(512), nullable=False),
    Column("source_column", String(256), nullable=False),
    Column("target_table", String(512), nullable=False),
    Column("target_column", String(256), nullable=False),
    Column("transformation", String(32), nullable=False),
    Column("transformation_subtype", String(32), nullable=False),
    Column("inferred", Boolean(), nullable=False, server_default=text("false")),
    Column("inference_status", String(32), nullable=False, server_default="confirmed"),
    Column("confidence", Numeric(5, 4), nullable=False, server_default="1.0"),
    Column("sql_hash", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint(
        "transformation IN ('DIRECT', 'INDIRECT')",
        name="transformation_is_supported",
    ),
    CheckConstraint(
        "transformation_subtype IN "
        "('DIRECT', 'AGGREGATION', 'TRANSFORMATION', 'FILTER', 'JOIN', 'CAST', 'EXPRESSION')",
        name="transformation_subtype_is_supported",
    ),
    CheckConstraint(
        "inference_status IN ('confirmed', 'inferred', 'rejected')",
        name="inference_status_is_valid",
    ),
    Index("ix_lineage_column_edges_source", "project_id", "source_table", "source_column"),
    Index("ix_lineage_column_edges_target", "project_id", "target_table", "target_column"),
    Index("ix_lineage_column_edges_run_id", "run_id"),
)


# L-7:整份血缘报告的 AI 解读(风险点 / 刷新模式解释 / 建议)。**纯追加** —— 每次
# ai-enrich 落一行,绝不回写 lineage_runs.parse_summary 的确定性字段。
lineage_ai_enrichments = Table(
    "lineage_ai_enrichments",
    metadata,
    Column("id", String(36), primary_key=True),
    Column(
        "run_id",
        String(36),
        ForeignKey("lineage_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "project_id",
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("interpretation", Text(), nullable=False),
    Column("provider", String(64), nullable=False),
    Column("model", String(128), nullable=False),
    Column("egress_level", Integer(), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Index("ix_lineage_ai_enrichments_run_created", "run_id", "created_at"),
)


# ─────────────────────────────────────────────────────────────────────────────
# workflows / workflow_templates —— Workflow 2.4 定义持久化(ADR-0009)
#   dag_jsonb = WorkflowSpec.model_dump(mode="json")(app/domain/workflow.py)
#   schedule_cron / schedule_enabled 冗余自 dag_jsonb.schedule,
#   供 PR-4 调度器 tick 扫表用(避免每 tick 全表解析 JSONB)
# ─────────────────────────────────────────────────────────────────────────────
workflows = Table(
    "workflows",
    metadata,
    Column("id", String(36), primary_key=True),
    Column(
        "project_id",
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("name", String(128), nullable=False),
    Column("dag_jsonb", JSONB(), nullable=False),
    # ★ 冗余列(权威数据在 dag_jsonb.schedule):调度器扫表专用,写入时同步
    Column("schedule_cron", String(64), nullable=True),
    Column("schedule_enabled", Boolean(), nullable=False, server_default=text("false")),
    # 幂等防重锚点(PR-4b):最近一次已触发的 cron 时刻。tick 只在
    # most_recent_fire(cron, now) > 此值时入队并推进该锚点 —— 同一触发点不重复,
    # 进程重启不补跑历史错过点(至多补当前触发点一次)。NULL = 从未触发(新调度
    # 首次见到只播种基线不触发,详见 app/services/workflow_scheduler.py)。
    Column("schedule_last_fired_at", DateTime(timezone=True), nullable=True),
    # ── C-10 SQL sensor 触发(数据到达触发 + 冷却期)──────────────────────────
    # sensor_enabled 冗余自 dag_jsonb.sensor.enabled,供调度器扫表(权威在 dag_jsonb)。
    # sensor_last_checked_at:上次派发 sensor 检查 job 的时刻(检查间隔节流锚点)。
    # sensor_last_triggered_at:上次 sensor 命中触发 workflow_run 的时刻(冷却期锚点;
    #   worker 侧带守卫的原子推进,冷却期内不重复触发)。均 NULL = 从未发生。
    Column("sensor_enabled", Boolean(), nullable=False, server_default=text("false")),
    Column("sensor_last_checked_at", DateTime(timezone=True), nullable=True),
    Column("sensor_last_triggered_at", DateTime(timezone=True), nullable=True),
    # 总开关:false 时调度器不触发(手动运行语义属 PR-3/PR-4)
    Column("enabled", Boolean(), nullable=False, server_default=text("true")),
    Column(
        "created_by",
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("project_id", "name", name="uq_workflows_project_name"),
    Index("ix_workflows_project_updated", "project_id", "updated_at"),
)


# workflow_templates —— 全局归属(跟随 1.x:config/workflow_templates.json
# 为全局单文件 JsonStore,无项目命名空间;V1_AS_IS §2.3)
workflow_templates = Table(
    "workflow_templates",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("name", String(128), nullable=False),
    Column("description", Text(), nullable=True),
    Column("dag_jsonb", JSONB(), nullable=False),
    Column(
        "created_by",
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("name", name="uq_workflow_templates_name"),
)


# ─────────────────────────────────────────────────────────────────────────────
# result_sets —— ★ R6 红线 DB 层:无 cursor 字段
# ─────────────────────────────────────────────────────────────────────────────
result_sets = Table(
    "result_sets",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("execution_id", String(36), nullable=False),
    Column(
        "console_id",
        String(36),
        ForeignKey("sql_consoles.id", ondelete="SET NULL"),
        nullable=True,
    ),
    # storage_ref:JSONB(backend + uri),指向 spool;不是 cursor 引用
    Column("storage_ref", JSONB(), nullable=False),
    Column("columns", JSONB(), nullable=False),
    Column("loaded_rows", Integer(), nullable=False, server_default="0"),
    Column("total_rows", Integer(), nullable=True),
    Column("state", String(32), nullable=False, server_default="streaming"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Index("ix_result_sets_console_updated", "console_id", "updated_at"),
    Index("ix_result_sets_execution_id", "execution_id"),
    CheckConstraint(
        "state IN ('streaming', 'complete', 'failed', 'closed')",
        name="state_is_valid",
    ),
)


# ─────────────────────────────────────────────────────────────────────────────
# compare_result_inputs —— SQL 结果到 Compare 的不可变 spool snapshot catalog
# ─────────────────────────────────────────────────────────────────────────────
compare_result_inputs = Table(
    "compare_result_inputs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column(
        "project_id",
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "created_by",
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("origin_kind", String(16), nullable=False),
    Column("origin_id", String(36), nullable=False),
    # provenance 只作证据锚点;源 result_sets 行可先于 snapshot tombstone 被清理。
    Column("source_result_set_id", String(36), nullable=False),
    Column("columns", JSONB(), nullable=False, server_default=text("'[]'::jsonb")),
    Column("loaded_rows", Integer(), nullable=False, server_default="0"),
    Column("total_rows", Integer(), nullable=True),
    Column("truncated", Boolean(), nullable=False, server_default=text("false")),
    Column("has_more", Boolean(), nullable=False, server_default=text("false")),
    Column("state", String(16), nullable=False, server_default="ready"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("deleted_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("origin_kind IN ('statement', 'job')", name="origin_kind_is_supported"),
    CheckConstraint("state IN ('ready', 'deleted')", name="state_is_supported"),
    Index("ix_compare_result_inputs_project_created", "project_id", "created_at"),
    Index("ix_compare_result_inputs_expiry", "state", "expires_at"),
    Index("ix_compare_result_inputs_origin", "origin_kind", "origin_id"),
)


# ─────────────────────────────────────────────────────────────────────────────
# license_state —— singleton(只一行 id=1)
# ─────────────────────────────────────────────────────────────────────────────
system_settings = Table(
    "system_settings",
    metadata,
    Column("key", String(128), primary_key=True),
    Column("value", Text(), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)


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


# ─────────────────────────────────────────────────────────────────────────────
# ai_configs —— AI Gateway singleton 配置(API key 只存 SecretRef)
# ─────────────────────────────────────────────────────────────────────────────
ai_configs = Table(
    "ai_configs",
    metadata,
    Column("id", Integer(), primary_key=True),
    Column("enabled", Boolean(), nullable=False, server_default=text("false")),
    Column("provider", String(32), nullable=False, server_default="off"),
    Column("model", String(128), nullable=True),
    Column("base_url", Text(), nullable=True),
    Column("api_key_secret_ref", String(64), nullable=True),
    Column("max_auto_egress_level", Integer(), nullable=False, server_default="0"),
    Column("l4_requires_optin", Boolean(), nullable=False, server_default=text("true")),
    Column("enable_inference", Boolean(), nullable=False, server_default=text("false")),
    Column("enable_auto_translation", Boolean(), nullable=False, server_default=text("false")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint("id = 1", name="singleton"),
    CheckConstraint(
        "provider IN ('off', 'mock', 'openai_compatible', 'anthropic', 'ollama')",
        name="provider_is_supported",
    ),
    CheckConstraint(
        "max_auto_egress_level >= 0 AND max_auto_egress_level <= 3",
        name="max_auto_egress_level_range",
    ),
)


__all__ = [
    "APPLICATION_SECRET_KINDS",
    "ai_configs",
    "audit_logs",
    "compare_result_inputs",
    "compare_tasks",
    "console_sessions",
    "console_statement_events",
    "console_statements",
    "datasources",
    "export_download_tokens",
    "job_events",
    "jobs",
    "license_state",
    "lineage_ai_enrichments",
    "lineage_column_edges",
    "lineage_edges",
    "lineage_runs",
    "metadata",
    "metadata_caches",
    "mfa_recovery_codes",
    "project_members",
    "projects",
    "result_sets",
    "revoked_tokens",
    "run_index",
    "secret_refs",
    "sql_consoles",
    "sql_templates",
    "system_settings",
    "uploads",
    "users",
    "workflow_templates",
    "workflows",
]
