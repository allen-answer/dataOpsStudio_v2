# Session Broker DM / MySQL 集成测试运行手册

本手册对应阶段 A7。测试只通过公开的 `InteractiveConnection`、`CancelChannel`、
`ErrorClassifier` 与 `SessionBroker` 接口验收行为，不读取或打印凭据。

## 覆盖范围

| 数据库 | 硬证据 |
|---|---|
| DM | `SP_CANCEL_SESSION_OPERATION` 自探测；长查询取消 `<1s`；owner 连接取消后续用；`-6515/-2501/-70028/-2104` 分类；broker deadline → SP_CANCEL → `timeout` 阶梯 |
| MySQL 8 | `KILL QUERY` → `1317` 且连接续用；`MAX_EXECUTION_TIME` → `3024`；`KILL CONNECTION` → `2013` 且连接判死 |

DM 用例同时带 `integration` 与 `dm_integration` marker。没有 DM 环境变量时全部 skip，
GitHub Actions 不连接 DM，R9 矩阵不变。MySQL 用例带 `integration` marker；现有
`mysql-integration` job 已提供 MySQL 8 service 并执行 `pytest -m integration -v`，因此 A7
不需要修改 `.github/workflows/ci.yml`，也就没有需要拆出的 CI PR。

## DM：本机 WSL SPIKE 靶子

目标是开发者本机自建的 SPIKE 库，默认监听 `127.0.0.1:5237`。它不是生产或内网实例。
端口可达性可先在 PowerShell 验证：

```powershell
Test-NetConnection -ComputerName 127.0.0.1 -Port 5237 -InformationLevel Quiet
```

WSL 中的 dmPython 2.5.32 环境入口为 `/tmp/dmenv.sh`，只用于本机验证：

```bash
source /tmp/dmenv.sh
/tmp/py312/python3.12 -c 'import dmPython; print("dmPython import OK")'
```

从仓库根目录运行测试。账号与口令必须由本机凭据注入器或当前进程环境提供，不写入
`.env`、shell history、pytest 参数、日志或 PR。取消测试复用 SPIKE 已有的百万行
`SYSDBA.SPIKE_BIG`；`-2104` 测试会创建并清理一个 `DOSV2_A7_` 前缀测试表。只有确认连接的
是可写、可丢弃的 SPIKE 库后，才设置 DDL 闸门：

```powershell
$env:DATAOPS_TEST_DM_HOST = "127.0.0.1"
$env:DATAOPS_TEST_DM_PORT = "5237"
$env:DATAOPS_TEST_DM_DATABASE = "SPIKE"
$env:DATAOPS_TEST_DM_USERNAME = "<由本机凭据注入>"
$env:DATAOPS_TEST_DM_PASSWORD = "<由本机凭据注入；不要把真实值写进历史>"
$env:DATAOPS_TEST_DM_SCHEMA = $env:DATAOPS_TEST_DM_USERNAME
$env:DATAOPS_TEST_DM_ALLOW_DDL = "1"
uv run pytest -m dm_integration tests/integration/test_session_broker_dm.py -v -s
```

`DATAOPS_TEST_DM_DATABASE` 标识本机实例的 DB_NAME；DM adapter 的
`DatasourceConnInfo.database` 语义是登录 schema，因此套件另读 `DATAOPS_TEST_DM_SCHEMA`，
未设置时默认等于 `DATAOPS_TEST_DM_USERNAME`。

验收输出必须出现：

- `DM SP_CANCEL code=-6515 elapsed=...s reuse=2`，且 elapsed `<1.000s`；
- `DM connection classification codes=-2501,-70028`；
- `DM same-batch classification code=-2104`；
- `DM timeout ladder=deadline->SP_CANCEL->timeout; session reused`；
- 全文件通过、无 skip。

若只需在生产内网做已批准的只读复核，不得设置 `DATAOPS_TEST_DM_ALLOW_DDL=1`；此时带 DDL
的长查询与 `-2104` 用例会 skip。生产复核仅覆盖连接、数值错误分类与 SP_CANCEL 权限探测，
并且要先获得所有者明确授权。

## MySQL：本地容器或 CI service

CI 自动运行，无需额外 workflow。手工复现时可复用任意临时 MySQL 8 容器；所有凭据只从
进程环境传入，容器销毁由操作者按本机数据保留策略执行：

```powershell
$env:DATAOPS_TEST_MYSQL_HOST = "127.0.0.1"
$env:DATAOPS_TEST_MYSQL_PORT = "3306"
$env:DATAOPS_TEST_MYSQL_USER = "<测试账号>"
$env:DATAOPS_TEST_MYSQL_PASSWORD = "<本机临时测试值>"
$env:DATAOPS_TEST_MYSQL_DATABASE = "<测试库>"
uv run pytest -m integration tests/integration/test_session_broker_mysql.py -v -s
```

验收输出必须出现 `1317 reuse=2`、`3024` 与 `2013 owner_dead=true`，且三条用例全部通过。

## 无真库时的安全检查

以下命令不连接任何数据库；DM/MySQL 文件会因缺少环境变量而 skip，但仍能证明 marker、
import、收集和静态规则没有漂移：

```text
uv run pytest tests/integration/test_session_broker_dm.py tests/integration/test_session_broker_mysql.py -q
uv run ruff check tests/integration/test_session_broker_dm.py tests/integration/test_session_broker_mysql.py
uv run ruff format --check tests/integration/test_session_broker_dm.py tests/integration/test_session_broker_mysql.py
```

任何失败报告只记录错误分类码与脱敏摘要，不复制驱动原始异常、连接串或环境变量值。
