"""pydantic-settings 配置层(契约 §1 + 设计稿 §3.1)。

★ R2 红线相关:本层**只持有 Bootstrap secret 文件路径**,不持有 secret 明文值。
实际的 master key / PG 密码 / license 文件由 BootstrapSecrets 实现按这些路径读取。

★ 三形态共用同一份 Settings,差异通过 form 字段 + env 覆盖实现:
- portable:Form.PORTABLE,默认 ai 关 / max_egress=L0
- onprem_docker:Form.ONPREM_DOCKER,docker secret/env 注入路径
- hosted:Form.HOSTED,KMS-based(实际路径会被 KmsBootstrapSecrets 忽略)
- dev:Form.DEV,job_backend=threadpool,单进程同窗

环境变量约定(env_prefix):
- DATAOPS_FORM / DATAOPS_JOB_BACKEND
- DATAOPS_BOOTSTRAP_MASTER_KEY_FILE / DATAOPS_BOOTSTRAP_PG_APP_PASSWORD_FILE / ...
- DATAOPS_PG_HOST / DATAOPS_PG_PORT / DATAOPS_PG_USER / DATAOPS_PG_DATABASE / ...
- DATAOPS_API_HOST / DATAOPS_API_PORT / DATAOPS_API_METRICS_PORT / DATAOPS_API_ENABLE_DOCS
- DATAOPS_FRONTEND_DIST(前端 dist 目录;设置即 API 进程伺服 SPA,默认 API-only)
- DATAOPS_WORKER_WORKER_ID / DATAOPS_WORKER_MAX_CONCURRENT_JOBS / ...
- DATAOPS_RESULT_BACKEND / DATAOPS_RESULT_LOCAL_ROOT / ...
- DATAOPS_AI_ENABLED / DATAOPS_AI_PROVIDER / DATAOPS_AI_MAX_AUTO_EGRESS_LEVEL / ...
- DATAOPS_LOG_LEVEL
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Form(StrEnum):
    """部署形态(契约 §1.2、设计稿 §1.2)。"""

    HOSTED = "hosted"
    ONPREM_DOCKER = "onprem_docker"
    PORTABLE = "portable"
    DEV = "dev"


class JobBackendKind(StrEnum):
    """JobBackend 实现选择(契约 §3.1、设计稿 §2.2.2)。

    POSTGRES = PostgresJobBackend(默认,所有形态都可用 + 推荐)
    THREADPOOL = ThreadPoolJobBackend(dev / 极简 portable 同进程)
    """

    POSTGRES = "postgres"
    THREADPOOL = "threadpool"


class BootstrapPaths(BaseSettings):
    """Bootstrap Secret 文件路径(只路径,不读值;契约 §3.3、设计稿 §7.3.1)。

    实际值由 BootstrapSecrets 实现读取。R8 红线:这些路径下的文件
    在 .gitignore 中精确忽略,绝不入 git。
    """

    model_config = SettingsConfigDict(env_prefix="DATAOPS_BOOTSTRAP_", extra="ignore")

    master_key_file: Path = Path("config/.secret_master.key")
    pg_app_password_file: Path = Path("config/secrets/pg_app_password")
    pg_superuser_password_file: Path = Path("config/secrets/pg_superuser_password")
    jwt_secret_file: Path = Path("config/secrets/jwt_secret")
    license_file: Path = Path("config/license.lic")


class MetadataDbConfig(BaseSettings):
    """PG 元数据库连接(★ 不持密码,密码走 BootstrapPaths.pg_app_password_file)。

    R2 红线:配置层不持有明文密码。连接时由 db client 读 BootstrapSecrets
    取密码组装 DSN。
    """

    model_config = SettingsConfigDict(env_prefix="DATAOPS_PG_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 15432  # 与 1.x 5432 / 1.x daily-report-bot 内部 5432 隔离
    user: str = "dataops"
    database: str = "dataops"

    # connection pool(设计稿 §4.4)
    pool_size: int = 10
    max_overflow: int = 20
    pool_recycle_seconds: int = 1800
    pool_pre_ping: bool = True


class ApiConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DATAOPS_API_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8020  # 与 1.x 8010 / daily-report-bot 8000 隔离
    metrics_port: int = 8021
    enable_docs: bool | None = None

    # 前端 SPA 静态产物目录(frontend/dist)。设置且目录存在(含 index.html)→
    # API 进程一并伺服前端(2.0 前后端同机同进程,零反代,见
    # docs/deployment/quickstart.md)。默认 None = API-only,行为与现状完全一致。
    # env: DATAOPS_FRONTEND_DIST(非 DATAOPS_API_ 前缀,故用 validation_alias)。
    frontend_dist: Path | None = Field(default=None, validation_alias="DATAOPS_FRONTEND_DIST")


class WorkerConfig(BaseSettings):
    """Worker 进程配置(契约 §3.1 JobBackend.heartbeat + 设计稿 §2.2.3)。"""

    model_config = SettingsConfigDict(env_prefix="DATAOPS_WORKER_", extra="ignore")

    worker_id: str = "worker-1"
    max_concurrent_jobs: int = 2
    heartbeat_interval_seconds: int = 15
    heartbeat_timeout_seconds: int = 600  # 慢首行 OLAP 查询优先避免误杀,见 ADR-0018
    poll_interval_seconds: float = 0.1  # T6:portable 小队列优先降低首个 job 体感延迟
    cancel_check_row_interval: int = 5000  # F3:cancel 查询节流间隔


class ResultStoreConfig(BaseSettings):
    """ResultStore + ResultSet spool 配置(设计稿 §2.6.4)。

    ★ R6 红线相关:cursor_max_hold_seconds 控制 DB cursor 最长持有,
    与"ResultSet 不持有 cursor"配合 —— worker 拉满即关 cursor,UI 走 spool。
    """

    model_config = SettingsConfigDict(env_prefix="DATAOPS_RESULT_", extra="ignore")

    backend: str = "local_fs"  # local_fs(2.0.0)/ s3(2.x hosted)
    local_root: Path = Path("data/results")

    spool_max_rows: int = 1_000_000
    spool_max_bytes: int = 512 * 1024 * 1024  # 512MB
    cursor_max_hold_seconds: int = 300
    result_ttl_days: int = 7
    max_active_resultsets_per_console: int = 3


class AiGatewayConfig(BaseSettings):
    """AI Gateway 配置(契约 §3.5,2.0.0 只做壳,默认关)。

    max_auto_egress_level:默认 L0,form 决定上限(设计稿 §2.7.5):
    - hosted: L1 (L2+ 需 admin / 用户 opt-in)
    - onprem_docker: L0(客户按需放开)
    - portable: L0(L4 默认完全禁止)
    """

    model_config = SettingsConfigDict(env_prefix="DATAOPS_AI_", extra="ignore")

    enabled: bool = False
    provider: str | None = None  # openai / anthropic / local / ...
    endpoint: str | None = None  # provider URL,on-prem 私有 LLM 时填
    max_auto_egress_level: int = 0  # 见 app.domain.ai.EgressLevel(L0-L5,L5 永禁)
    l4_requires_optin: bool = True


class LoggingConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DATAOPS_LOG_", extra="ignore")

    level: str = "INFO"


class Settings(BaseSettings):
    """顶层配置 —— pydantic-settings + env vars。

    实例化时各 sub-config 通过 default_factory 触发 __init__,各自从对应
    env_prefix 读 env。调用方惯例:在进程启动时调一次 load_settings(),
    后续复用结果(本模块不做缓存,由调用方控制)。
    """

    model_config = SettingsConfigDict(env_prefix="DATAOPS_", extra="ignore")

    form: Form = Form.DEV
    job_backend: JobBackendKind = JobBackendKind.POSTGRES

    bootstrap: BootstrapPaths = Field(default_factory=BootstrapPaths)
    db: MetadataDbConfig = Field(default_factory=MetadataDbConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    result_store: ResultStoreConfig = Field(default_factory=ResultStoreConfig)
    ai: AiGatewayConfig = Field(default_factory=AiGatewayConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def load_settings() -> Settings:
    """加载配置(每次构造新实例,读 env)。

    调用方惯例:进程启动时调一次,后续传引用。本模块不做 lru_cache 因为
    测试需要按需 reset。Codex 实现入口 main.py / worker.py / launcher.py
    时各自调用一次。
    """
    return Settings()
