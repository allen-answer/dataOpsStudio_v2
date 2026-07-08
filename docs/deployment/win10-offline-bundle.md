# Win10 x64 全离线部署包

面向**无网络的 Windows 10 x64** 目标机的一键部署包:一个 zip,解压后运行
`start.bat` 即起完整 DataOpsStudio(API + worker + 内置 PostgreSQL + 各数据库驱动
+ 前端),目标机无需预装 Python / PostgreSQL / Node,无需联网。

- 打包脚本:[`tools/package/build_win10_bundle.py`](../../tools/package/build_win10_bundle.py)
- 面向最终用户的中文说明随包发布(bundle 内 `README.md`)。

## 1. 包内容

| 组成 | 来源 | 说明 |
|---|---|---|
| 内置 Python 3.12 | python-build-standalone(经 `uv python`) | 可重定位,含 pip;目标机不装 Python |
| `uv.exe` | 构建机 uv | 仅用于 launcher 的 `uv run` 子进程,离线只执行本机 `.venv` |
| 依赖 wheels | `uv export` + `pip download`(cp312 win_amd64) | 全量,含 psycopg[binary]、pymysql、oracledb(thin)、ibm_db、dmPython |
| PostgreSQL 16 | EDB `postgresql-16.14-1-windows-x64-binaries.zip` | 精简为 `bin/lib/share`(丢弃 pgAdmin/StackBuilder/doc/include) |
| 前端 SPA | `npm ci && npm run build` 产出的 `frontend/dist` | 由 API 进程同端口伺服 |
| 应用源码 + `uv.lock` | 仓库 | 运行 `python -m app.*` 与 alembic;lock 保证可复现 |
| `start.bat` / `stop.bat` / `README.md` | 脚本生成 | 首次初始化 + 启动 / 优雅停止 / 中文说明 |

**零凭据(R8):** 包内不含 master key、PG 密码、admin 密码、license。这些都在目标机
**首次运行时**生成或交互输入,绝不进包、绝不进 git。

## 2. 运行期形态(为什么这样设计)

launcher(`app/launcher.py`)以 `uv run python -m app.main` / `uv run alembic ...`
形式拉起子进程,因此包内带 `uv.exe`,但**全程离线**:

- `start.bat` 设 `UV_OFFLINE=1 UV_NO_SYNC=1 UV_FROZEN=1 UV_PYTHON_DOWNLOADS=0`,
  `uv run` 只执行首次运行时离线构建好的 `.venv`,不联网、不改 lock。
- `.venv` 在**目标机**首次运行时用内置 Python + 本地 wheels 离线创建
  (`python -m venv` + `pip install --no-index --find-links runtime\wheels`),
  因此 venv 内的绝对路径天然对目标机正确(无重定位问题)。
- PostgreSQL 由 launcher 以 `--pg-bin-dir pgsql\bin` 托管(initdb/pg_ctl/postgres)。

这样对已 review 的 launcher / app 代码**零改动**,打包与首次初始化全部在外围脚本完成。

## 3. 构建(在有网络的构建机)

前置:`uv`、Node/npm、网络。

```bash
uv run python tools/package/build_win10_bundle.py
```

产物在 `dist-bundle/`(git-ignored):

- `dataops-studio-<ver>-win10-x64-offline.zip` —— 交付包
- `*.MANIFEST.txt` —— 版本、PG 版本、zip 大小、zip sha256

下载物缓存在 `dist-bundle/cache/`(wheels / PG zip / python / uv),重复构建可复用。
PG zip 的 sha256 在脚本内 pin,构建时打印实际值供比对。

常用选项:

- `--skip-frontend-build` 复用现有 `frontend/dist`,不跑 npm。
- `--keep-staging` 保留解压态的 staging 目录。
- `--cache-dir` / `--output-dir` 自定义缓存与产物位置。

## 4. 目标机部署

解压 zip,运行 `start.bat`。首次运行自动:构建 `.venv` → 生成 bootstrap 密钥 →
初始化并启动 PG → 迁移到最新版本 → 创建管理员(交互输入密码)→ 启动 API + worker。
之后再运行 `start.bat` 只做启动。

可配置环境变量:`DATAOPS_API_PORT`(默认 8020)、`DATAOPS_PG_PORT`(默认 15432)、
`DATAOPS_HOME`(默认 `<包>\home`)、`DATAOPS_ADMIN_USER`、`DATAOPS_ADMIN_PASSWORD`
(设置则非交互创建管理员)。停止用 `stop.bat`(`--force` 为紧急停止)。

**路径长度注意**:内置 PG 的本地 socket 路径(`<DATAOPS_HOME>\data\pg-socket\...`)
有 107 字节上限;解压/实例路径过深会让 `pg_ctl start` 失败(initdb 正常,pg.log
报 Unix socket 路径过长)。建议解压到短路径(如 `D:\dataops\`)。

## 5. 达梦(DM)注记

包内**含 dmPython 驱动 wheel**(它是 locked 主依赖,有 cp312 win_amd64 wheel),
但**不含达梦专有客户端**。要连 DM 数据源,需在目标机另装 DM 客户端并设
`DM_HOME`、把 `%DM_HOME%\bin` 加进 `PATH`(worker 进程需能 dlopen DM 加密库),
否则报 `-70089`。根因与完整修法见
[`quickstart.md`](./quickstart.md) 的「DM 数据源注记」与
[`../acceptance-dm-certified.md`](../acceptance-dm-certified.md)。

## 6. 升级

用新版包替换 `app/`、`frontend/dist/`、`runtime/wheels/`、`runtime/requirements-frozen.txt`,
删除旧 `.venv/`(下次 `start.bat` 离线重建),保留 `home/`(实例数据),再运行
`start.bat`(自动跑新迁移)。**升级前备份 `home/`。**

## 7. 安全

- `home/config/.secret_master.key` 是所有数据源密码的加密根;**离线安全备份**,
  丢失即无法解密已存凭据。它不在包内、不进 git。
- 管理员 / 数据源密码只以 bcrypt 哈希、Fernet 密文存库,不落明文文件(R2)。
