# DM adapter 真实例验证(DM Beta → Certified)

> 执行:review agent + 人(2026-06-11)。真实例:达梦 DM8(64 V8),WSL Ubuntu-26.04
> 内 127.0.0.1:5236,用户 ods(DBA)。这是 backlog「DM Certified 宣称前必做」条目的
> hard evidence —— DM adapter 此前(#25)按 `docs/legacy/V1_AS_IS.md` 文档撰写,
> **从未对真实例跑过**;本次真机验证暴露并修复了 3 个 adapter bug。

## 验证方式

- 测试:`tests/integration/test_dm_real_instance.py`(env-gated,缺 `DATAOPS_TEST_DM_*`
  即 skip,不进 CI;GH Actions 无可信 DM 镜像)
- 8 项覆盖 backlog 要求的全部维度:连接 / 服务器版本 / 错误凭据分类 / 流式 SELECT
  (2500 行跨多 fetchmany 批次)/ 软取消 / kill_query 能力申报 / introspection
  (schema/table/column/index/PK)/ DBMS_METADATA DDL / EXPLAIN。
- 结果:**8 passed**(server_version=DM V8;流式列类型 ID=integer/LABEL=string/
  AMOUNT=decimal/CREATED_AT=datetime;introspection 列类型与 PK 正确;explain 4 行)。

## 真机暴露并修复的 3 个 adapter bug

| # | 现象 | 根因 | 修复 |
|---|---|---|---|
| 1 | INT 列映射成 DECIMAL、DECIMAL(10,2) 映射成 UNKNOWN | dmPython `cursor.description[1]` 是 **type-object**(`dmPython.NUMBER`/`DECIMAL`...)非整数码;INT 报 NUMBER,NUMBER/DECIMAL 无法只凭 type-object 区分整数/小数 | `dm_types.py` 新增 `description_item_to_column_type(code, scale)`:NUMBER/DECIMAL 按 `description.scale`(idx5)判别,scale==0→INTEGER 否则 DECIMAL;补全 BIGINT/DOUBLE/REAL/FIXED_STRING/LONG_*/LOB 等真实 type-object 映射 |
| 2 | introspection `KeyError: 'name'` | DM 把不带引号的别名 `AS name` 折叠成大写键 `NAME`,而 introspection 全程按小写键取值 | `dm_adapter.py:_query_dicts` 统一把 description 列名小写化(一处修,list_schemas/tables/columns/indexes/PK 全链路受益) |
| 3 | EXPLAIN 报 `-2002 Try to execute unprepared SQL` | DM 执行计划语法是 `EXPLAIN FOR <sql>`,非裸 `EXPLAIN <sql>` | `dm_adapter.py:explain()` 改 `EXPLAIN FOR {sql}` |

## 环境注记(部署相关,非 adapter bug)

- **dmPython 加密模块 `-70089`**:PyPI `dmpython` 轮子 bundled 的加密库缺传递依赖,
  `dlopen` 加密模块失败。修法:清掉轮子 `dmpython.libs/` 内的孤立加密库,设
  `DM_HOME=/opt/dmdbms` + `LD_LIBRARY_PATH=/opt/dmdbms/bin:/opt/dmdbms/bin/external_crypto_libs`,
  让驱动用 DM 客户端安装目录下「依赖完整」的全套库。**on-prem 部署 DM 数据源时
  worker 进程需同样配置**(应进部署文档,见 backlog 后续项)。

## 口径升级

DM adapter 已具备真实例 hard evidence,从 **DM Beta** 升级为 **DM Certified**
(MySQL + DM Certified)。Oracle 仍 2.0.x、DB2 Preview 不变。
