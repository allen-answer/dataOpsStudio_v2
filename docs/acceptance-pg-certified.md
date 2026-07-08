# PostgreSQL adapter 真实例验证(PG Beta → Certified 前置)

> 执行:test agent(Claude Fable 5)。真实例:GitHub Actions CI 的 `postgres:16-alpine`
> service(`PG Queue Concurrency` job 注入 `DATAOPS_TEST_PG_URL`)。这是 backlog
> 「PostgreSQL adapter Certified 前置项」(#98 评论,PG-1/PG-2)的 hard evidence 起点 ——
> PG adapter(#98)按契约 + psycopg3 文档撰写,`statement_timeout` 静默失效 bug 合并前
> 已在单测层修过(set_config 参数化路径),`cancel_safe`(#144)刚接线;本轮把验证清单
> 固化成 **PG-gated 集成测试**,由 CI 真 PG16 实例提供在真库上跑通的例证。

## 验证方式

- 测试:`tests/integration/test_pg_adapter_certified.py`(env-gated,缺 `DATAOPS_TEST_PG_URL`
  即整文件 skip)。文件带 `pytest.mark.integration`,落 `tests/integration/`,由 CI 的
  `PG Queue Concurrency` job(`uv run pytest -m integration tests/integration/ -v`,已启用)
  自动收集执行 —— 该 job 的 `postgres:16-alpine` service + `DATAOPS_TEST_PG_URL` 环境变量
  即为真实例来源。本地无 PG / 无 docker,故本机口径 = `mypy` + `ruff` 通过 + 集成测试
  正确 skip;真运行例证以 CI job log 为准(见 PR body 附的 run 链接与 stdout)。
- 7 项覆盖 backlog #98 要求的全部维度 + 常见类型全景:连接 / 错误凭据分类(不泄密) /
  statement_timeout 真触发 / jsonb·json 列 / information_schema 注入面 / cancel_safe 真取消
  与连接干净 / numeric·timestamptz·bytea·array 类型映射。

## 验收清单(每项 → 覆盖测试)

| # | backlog 维度 | 验收判据 | 覆盖测试 | 状态 |
|---|---|---|---|---|
| 1 | 连接 + 版本 | `test_connection()` True 且拿到 `server_version` | `test_connection_and_server_version` | ☑ 待 CI 例证 |
| 2 | 错误凭据分类 | 错密码 → False + 结构化 `last_connection_error`,**明文密码不入错误串**(R5) | `test_wrong_password_classified_not_leaked` | ☑ 待 CI 例证 |
| 3 | **statement_timeout 真触发** | 1s 超时跑 `pg_sleep(3)` → 结构化 `QueryCanceled`(SQLSTATE **57014**),且 `elapsed < 3s`(证明 set_config 未静默失效) | `test_statement_timeout_fires_structured_error` | ☑ 待 CI 例证 |
| 4 | jsonb / json 列 | cursor 描述 + introspection 两路映射均为 `ColumnType.JSON`;值反序列化为 Python `dict` / `list` | `test_jsonb_json_column_type_and_value_serialization` | ☑ 待 CI 例证 |
| 5 | information_schema 注入面 | 内嵌 `"` `;` `--` 的真实表名经参数化 introspection 原样 round-trip;恶意 schema/table 入参被标识符 guard 拒;注入尝试后原表仍在 | `test_introspection_special_identifier_escaped_and_no_injection` | ☑ 待 CI 例证 |
| 6 | cancel_safe 真取消(#144) | `pg_sleep(30)` 发 cancel → `QueryCancelledError` 且 `elapsed < 15s`;`pg_stat_activity` 无残留在飞查询;后续查询正常返回 | `test_server_side_cancel_is_clean` | ☑ 待 CI 例证 |
| 7 | 常见类型全景 | numeric→DECIMAL / timestamptz→DATETIME / bytea→BYTES / int[]→UNKNOWN(留 driver_type),流式 + introspection 两路一致;值形态 `Decimal`/tz-aware `datetime`/`bytes`/`list` | `test_common_type_panorama_mapping` | ☑ 待 CI 例证 |

> 「待 CI 例证」= 测试已写好、`mypy`/`ruff` 通过、本地正确 skip;真库跑通的 8/pass 例证
> 需 CI 的 PG16 job 跑完贴 log。合并前请确认该 job 收集到本文件的 7 个测试并全绿。

## 设计注记

- **int[](array)映射 UNKNOWN 是有意的,非 bug**:契约 `ColumnType` 只有 11 个统一枚举,
  无 ARRAY 项;按「无法识别的类型码一律落 UNKNOWN,不臆造,留 `driver_type` 供人工核对」
  的既定语义,array 列 introspection 落 `UNKNOWN` + `driver_type` 保留 `udt_name`(如 `_int4`)。
  测试对此显式断言,固化为期望行为。
- **注入面本质**:introspection 全部走 `%s` 参数化 WHERE(值绑定,不拼 SQL)+ 标识符入参
  `_validate_identifier`(`^[A-Za-z0-9_.$-]+$`)双保险。带引号/分号/注释符的名字要么作为
  **数据**安全 round-trip(表名来自库),要么作为**入参**被 guard 拒 —— 两条路径均无注入。

## 剩余项(Certified 正式宣称前)

1. **真客户实例走查**:CI 的 `postgres:16-alpine` 是干净默认实例。真实客户 PG 常有网络
   形态(SSL/`sslmode`、连接池中间件 PgBouncer 的 extended-protocol 差异)、权限形态
   (只读角色、schema 可见性受限、`pg_stat_activity` 需 `pg_monitor` 才能看别人的 query)
   差异 —— 这些差异**只有真客户环境能覆盖**,建议首个 PG 数据源真实用户接入时补一轮走查。
2. **前端执行白名单**:已确认 `frontend/src/views/SqlWorkspaceView.vue`
   `SUPPORTED_EXECUTION_DB_TYPES = new Set(['mysql', 'dm', 'postgresql'])` **已含 `postgresql`**
   (#63 建立的白名单),无需额外前端改动即可在 SQL Workspace 选 PG 数据源执行。

## 口径升级(达成条件)

CI 的 PG16 job 收集到本文件 7 个测试并全绿后,PG adapter 具备真实例(CI 形态)hard
evidence,可从 **PG Beta** 升级为 **PG Certified**;真客户实例走查(剩余项 1)完成后为完全
Certified。MySQL + DM Certified、Oracle 仍 2.0.x、DB2 Preview 不变。
