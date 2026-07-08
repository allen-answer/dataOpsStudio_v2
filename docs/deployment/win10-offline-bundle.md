# Win10 x64 部署包(offline / online 双模式)

面向 **Windows 10 x64** 目标机的一键部署包。**同一套 PowerShell 脚本**
(`install.ps1` / `start.ps1` / `stop.ps1` + 一行 `start.cmd` / `stop.cmd` 双击 shim)
覆盖两种发行形态,解压后运行 `start.cmd` 即起完整 DataOpsStudio(API + worker +
内置 PostgreSQL + 各数据库驱动 + 前端),目标机无需预装 Python / PostgreSQL / Node。

- **offline**:zip 内嵌全部依赖,**无网络**机器解压即用。
- **online**:轻量 zip(源码 + 前端 dist + 脚本 + 锁文件 + 下载清单),**首次运行联网**
  下载运行时/依赖/PG 并逐个 sha256 校验;下载完成后的准备与启动流程与 offline 完全一致。

- 打包脚本:[`tools/package/build_win10_bundle.py`](../../tools/package/build_win10_bundle.py)
- 面向最终用户的中文说明随包发布(bundle 内 `README.md`)。

## 1. 包内容

| 组成 | offline | online | 说明 |
|---|---|---|---|
| 内置 Python 3.12 | 内嵌 `runtime/python/` | 首次运行下载 | python-build-standalone(可重定位,含 pip) |
| `uv.exe` | 内嵌 `runtime/uv/` | 首次运行下载 | 仅用于 launcher 的 `uv run` 子进程,离线只执行本机 `.venv` |
| 依赖 wheels | 内嵌 `runtime/wheels/` | 首次从 PyPI 装 | 均按 `requirements-frozen.txt` 的 sha256 校验 |
| PostgreSQL 16 | 内嵌 `pgsql/`(slim) | 首次运行下载 | EDB `postgresql-16.14-1-...zip`,精简为 `bin/lib/share` |
| 下载清单 | — | `runtime/download-manifest.json` | 三个构件的 URL + sha256 pin |
| `requirements-frozen.txt` | ✔ | ✔ | `uv export`(含 sha256) |
| 前端 SPA `frontend/dist` | ✔ | ✔ | `npm ci && npm run build`,API 同端口伺服 |
| 应用源码 + `uv.lock` | ✔ | ✔ | 运行 `python -m app.*` 与 alembic |
| PowerShell 脚本 + `.cmd` shim + `README.md` | ✔ | ✔ | 脚本生成 |

**零凭据(R8):** 包内不含 master key、PG 密码、admin 密码、license。这些都在目标机
**首次运行时**生成或交互输入,绝不进包、绝不进 git。

## 2. 运行期形态(为什么这样设计)

launcher(`app/launcher.py`)以 `uv run python -m app.main` / `uv run alembic ...`
形式拉起子进程,因此包内(或下载)带 `uv.exe`,但**运行期全程离线**:

- `_common.ps1` 设 `UV_OFFLINE=1 UV_NO_SYNC=1 UV_FROZEN=1 UV_PYTHON_DOWNLOADS=0`,
  `uv run` 只执行首次运行时构建好的 `.venv`,不联网、不改 lock(两种模式一致)。
- `.venv` 在**目标机**首次运行时用运行时 Python + wheels 创建
  (offline `pip install --no-index --find-links runtime\wheels`;online 从 PyPI,
  两者均 `--require-hashes`),因此 venv 内的绝对路径天然对目标机正确。
- PostgreSQL 由 launcher 以 `--pg-bin-dir pgsql\bin` 托管(initdb/pg_ctl/postgres)。

**脚本形态(告别 batch 引号坑):** 核心逻辑全在 PowerShell,路径一律用单个变量,
native 工具用调用符 `&` 调起(`& $VenvPy -m app.launcher --root $env:DATAOPS_HOME ...`),
PowerShell 自己对每个参数加引号,不存在嵌套引号拼接。`.cmd` 只是一行 shim
转调对应 `.ps1`(带 `-ExecutionPolicy Bypass`,只对该次调用生效)。

这样对已 review 的 launcher / app 代码**零改动**,打包与首次初始化全部在外围脚本完成。

## 3. 构建(在有网络的构建机)

前置:`uv`、Node/npm、网络。

```bash
uv run python tools/package/build_win10_bundle.py                # offline(默认)
uv run python tools/package/build_win10_bundle.py --mode online  # online
```

产物在 `dist-bundle/`(git-ignored):

- `dataops-studio-<ver>-win10-x64-{offline,online}.zip` —— 交付包
- `*.MANIFEST.txt` —— 版本、各运行时来源、zip 大小、zip sha256

offline 的下载物缓存在 `dist-bundle/cache/`(wheels / PG zip / python / uv),重复构建可复用。
online 不下载任何运行时,只把 `download-manifest.json` 写进包;PG / python / uv 的
URL + sha256 在脚本内 pin(`PG_*` / `PYTHON_STANDALONE_*` / `UV_*` 常量),
构建时对 offline 的 PG zip 打印实际 sha256 供比对。

常用选项:

- `--skip-frontend-build` 复用现有 `frontend/dist`,不跑 npm。
- `--keep-staging` 保留解压态的 staging 目录。
- `--cache-dir` / `--output-dir` 自定义缓存与产物位置。

### 刷新 online 下载 pin

`PYTHON_STANDALONE_*` 需与 uv 在构建机管理的 CPython 3.12 patch 版本**对齐**
(取自 uv `download-metadata.json` 对应条目的 `url` + `sha256`),`UV_*` 与构建机 uv
版本对齐。升级 uv / Python 时同步这几个常量,否则 online 装出来的运行时与 offline 漂移。

## 4. 目标机部署

解压 zip,双击 `start.cmd`(或在目录内 `.\start.ps1`)。首次运行自动:
(online 先按清单下载运行时并校验 sha256 →)构建 `.venv` → 生成 bootstrap 密钥 →
初始化并启动 PG → 迁移到最新版本 → 创建管理员(交互输入密码)→ 启动 API + worker。
之后再运行只做启动。也可单独先跑 `install.ps1` 只做运行时准备(不启动服务)。

**ExecutionPolicy:** 直接右键跑 `.ps1` 可能被默认执行策略拦下。推荐双击 `start.cmd`
(shim 已带 `-ExecutionPolicy Bypass`,只对该次调用生效,不改系统策略);或手动
`powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1`。

可配置环境变量:`DATAOPS_API_PORT`(默认 8020)、`DATAOPS_PG_PORT`(默认 15432)、
`DATAOPS_HOME`(默认 `<包>\home`)、`DATAOPS_ADMIN_USER`、`DATAOPS_ADMIN_PASSWORD`
(设置则非交互创建管理员)。停止用 `stop.cmd`(`--force` 为紧急停止)。

**路径长度注意(短路径)**:内置 PG 的本地 socket 路径(`<DATAOPS_HOME>\data\pg-socket\...`)
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

用新版包替换 `app/`、`frontend/dist/`、`runtime/requirements-frozen.txt`
(offline 另换 `runtime/wheels/`;online 另换 `runtime/download-manifest.json`),
删除旧 `.venv/`(下次 `start` 重建),保留 `home/`(实例数据),再运行 `start`
(自动跑新迁移)。**升级前备份 `home/`。**

## 7. 安全

- `home/config/.secret_master.key` 是所有数据源密码的加密根;**离线安全备份**,
  丢失即无法解密已存凭据。它不在包内、不进 git。
- 管理员 / 数据源密码只以 bcrypt 哈希、Fernet 密文存库,不落明文文件(R2)。
