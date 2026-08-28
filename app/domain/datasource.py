"""业务数据库 datasource 连接信息(契约 §3.2 配套)。

所有 DatabaseAdapter 实现的构造器接受统一的 DatasourceConnInfo,1 个实例
对应 datasources 表 1 行。这避免每个 adapter 自定义构造签名导致的:
- 调用方代码与每个 adapter 耦合
- adapter 工厂 / 注册表难以写
- datasources 表 schema 变更需要改每个 adapter

★ password_ref.kind 必须是 DATASOURCE_PASSWORD,与 T3 R4 一脉相承
  (密码分类不能错位)。

★ frozen=True:连接信息是不可变值对象。换连接 = 新 instance + 重新 connect,
  不允许就地改字段(避免 cursor / 池中连接对象状态错乱)。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.secret import SecretKind, SecretRef


class DbType(StrEnum):
    """业务数据库方言(契约 §3.2、设计稿 §2.9)。

    2.0.0 推进顺序:MYSQL → DM → ORACLE(可滑 2.0.x)→ DB2(Preview)。
    POSTGRESQL 是元数据库的同时也可作为业务 datasource(自然就绪)。

    新增方言需要同步:
    1. 此 enum 增枚举
    2. app/dbclients/<name>_adapter.py 实现
    3. 适配 1.x AS-IS §2 已记录的方言坑(标识符大小写 / 分页 / cursor 语义)
    """

    MYSQL = "mysql"
    ORACLE = "oracle"
    DM = "dm"
    DB2 = "db2"
    POSTGRESQL = "postgresql"


class OperationPolicy(BaseModel):
    """Datasource operation policy(设计稿 §4.5 / PRD §3 权限面板).

    2.0.0 仅执行 allow_select / allow_explain;其余开关先作为显式策略落库,
    供 2.1+ 能力接入时复用同一校验入口。
    """

    allow_select: bool = True
    # EXPLAIN 与 SELECT 同为只读操作、同风险级,默认放开(0031)
    allow_explain: bool = True
    allow_dm_explain: bool = False
    allow_oracle_plan_table: bool = False
    allow_schema_import: bool = False
    allow_schema_save: bool = False
    allow_scenario_write: bool = False
    allow_record_task: bool = False


class DatasourceConnInfo(BaseModel):
    """业务数据库连接信息 —— 所有 adapter 共用入参。

    持久化对应见 app/db/models.py datasources 表 + 设计稿 §5.1:
      host          → datasources.host
      port          → datasources.port
      username      → datasources.username
      database      → datasources.database_name(★ 列名 database_name,
                       因 `database` 是 SQL 保留字,做列名 quote 易埋坑)
      password_ref  → datasources.password_secret_ref + kind 固定 DATASOURCE_PASSWORD
      db_type       → datasources.db_type
      extra         → datasources.capability_profile 的 connection 子节
                       (Oracle service_name / MySQL ssl_mode 等 adapter 私有)
    """

    model_config = ConfigDict(frozen=True)

    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    username: str = Field(min_length=1)
    database: str | None = Field(default=None, min_length=1)
    password_ref: SecretRef
    db_type: DbType
    extra: dict[str, Any] = Field(default_factory=dict)
    operation_policy: OperationPolicy = Field(default_factory=OperationPolicy)

    @field_validator("password_ref")
    @classmethod
    def _password_ref_must_be_datasource_password(cls, v: SecretRef) -> SecretRef:
        """密码分类不能错位 —— 与 T3 R4 一脉相承。"""
        if v.kind is not SecretKind.DATASOURCE_PASSWORD:
            raise ValueError(
                f"DatasourceConnInfo.password_ref.kind must be DATASOURCE_PASSWORD; "
                f"got {v.kind.value}"
            )
        return v
