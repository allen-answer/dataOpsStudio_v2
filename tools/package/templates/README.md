# DataOpsStudio 2.0 — Win10 x64 部署包

本包用 **PowerShell** 脚本(`start.ps1` / `stop.ps1` / `install.ps1`)完成安装与启停,
另附一行的 `start.cmd` / `stop.cmd` 双击 shim。有两种发行形态,**共用同一套脚本**:

- **offline(全离线)**:zip 内嵌 Python 运行时、全量依赖 wheel、PostgreSQL 16、前端 dist。
  拷到**没有网络**的机器上解压即可运行,首次运行离线建环境。
- **online(在线轻量)**:zip 只含源码 + 前端 dist + 安装/启动脚本 + 锁文件 + 下载清单
  (`runtime/download-manifest.json`,内含各构件的 URL 与 sha256)。首次运行**联网下载**
  Python 运行时、依赖(PyPI,按 `requirements-frozen.txt` 的 sha256 校验)、PostgreSQL 16
  (同一 EDB 地址 + sha256 校验),下载完的准备/启动流程与 offline 完全一致。

> 当前包形态见文件名:`...-win10-x64-offline.zip` 或 `...-win10-x64-online.zip`。
> online 首次运行需要外网可达 GitHub(python-build-standalone / uv 发布页)、PyPI、EDB。

若包内有 `DataOpsStudio.exe`(文件名含 `-gui`),优先双击它。桌面壳会在首启时收集
管理员密码、等待 `/healthz` 最长 900 秒,并在关窗时先优雅停止、超时后有界强杀兜底。
无 GUI 的脚本包仍从 `start.cmd` 启动。

## 系统要求

- Windows 10 x64(或更高),自带 PowerShell 5.1 与 `tar.exe`(1803+)
- offline 约 2GB 可用磁盘;online 首次下载约 0.6GB,安装后同量级
- 无需管理员权限;**不要用 root/系统管理员账户跑**(initdb 拒绝)

## ExecutionPolicy 说明

首次直接右键运行 `start.ps1` 可能被默认执行策略拦下。两种做法任选:

- **双击 `start.cmd`**(推荐):shim 里已带 `-ExecutionPolicy Bypass`,只对这一次调用生效,
  不改系统策略。内容仅一行:
  `powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*`
- **手动**:在本目录开 PowerShell 执行
  `powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1`
  或先 `Set-ExecutionPolicy -Scope Process Bypass` 再 `.\start.ps1`。

## 目录结构

```
dataops-studio-<ver>-win10-x64-<mode>/
  start.cmd / stop.cmd    双击 shim(一行,转调对应 .ps1,带 ExecutionPolicy Bypass)
  start.ps1               首次初始化 + 启动(API + worker + 内置 PostgreSQL)
  stop.ps1                优雅停止(活跃查询会先跑完)
  install.ps1             运行时准备(offline 建 venv;online 下载运行时/依赖/PG 后建 venv)
  _common.ps1             共享路径与 launcher 装配(offline/online 不分叉)
  app/                    应用源码
  frontend/dist/          前端 SPA(由 API 进程同端口伺服)
  runtime/requirements-frozen.txt   锁定依赖(含 sha256)
  runtime/download-manifest.json    仅 online:Python/uv/PG 的 URL + sha256
  pgsql/                  仅 offline:内置 PostgreSQL 16(bin/lib/share);online 下载后生成
  runtime/python/         仅 offline:内置 Python 3.12(可重定位);online 下载后生成
  runtime/uv/uv.exe       仅 offline:进程管理;online 下载后生成
  runtime/wheels/         仅 offline:全量依赖 wheel;online 首次从 PyPI 装
  .venv/                  首次运行创建(不在包内)
  home/                   运行时实例数据:config/ data/ logs/(首次运行创建)
```

## 首次初始化并启动

双击 `start.cmd`(或在本目录运行 `.\start.ps1`)。首次运行会自动:

1. 准备运行时(offline:直接用包内;online:按下载清单联网下载 Python/uv/PG 并逐个校验 sha256);
2. 用运行时 Python 离线创建 `.venv` 并安装全部依赖(offline `--no-index` 从包内 wheel;
   online 从 PyPI,均 `--require-hashes` 按 sha256 校验);
3. 生成 bootstrap 密钥(master key / PG 密码 / JWT);
4. 初始化并启动内置 PostgreSQL;
5. 执行数据库迁移到最新版本;
6. 创建管理员账户 —— **会提示你输入管理员密码**(密码只写入数据库 bcrypt 哈希,不落任何文件);
7. 启动 API + worker。

启动后浏览器打开 `http://127.0.0.1:8020/` 即为完整前端 + 后端。

也可单独先跑 `install.ps1` 只做运行时准备(不启动服务),再择时 `start.ps1`。

### 可配置项(启动前设环境变量)

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATAOPS_API_PORT` | `8020` | API/前端端口 |
| `DATAOPS_PG_PORT` | `15432` | 内置 PostgreSQL 端口 |
| `DATAOPS_HOME` | `<包>\home` | 实例数据目录(config/data/logs) |
| `DATAOPS_ADMIN_USER` | `admin` | 首次创建的管理员用户名 |
| `DATAOPS_ADMIN_PASSWORD` | (交互输入) | 设置后非交互创建管理员(自动化用) |

### 实例路径

Windows 内置 PostgreSQL 只监听 loopback TCP,不使用 Unix socket;`DATAOPS_HOME` 可包含空格
或较深路径。Linux Mint 包仍会在安装时检查 Unix socket 的 107 字节路径上限。

## 停止

运行 `stop.cmd`(或 `.\stop.ps1`)。优雅停止会等 worker 当前任务跑完;紧急停止用
`stop.cmd --force`(或 `.\stop.ps1 --force`)。

## 升级

用新版包替换 `app/`、`frontend/dist/`、`runtime/requirements-frozen.txt`
(offline 另换 `runtime/wheels/`;online 另换 `runtime/download-manifest.json`),
删除旧 `.venv/`(下次 `start` 会重建),保留 `home/`(实例数据),再运行 `start`
(会自动跑新迁移)。升级前请先备份 `home/`。

## 达梦(DM)数据源

本包含 dmPython 驱动 wheel,但**不含达梦专有客户端**。连接 DM 数据源需在目标机
另装 DM 客户端并设置 `DM_HOME` 及 `PATH`(加 `%DM_HOME%\bin`),否则 DM 加密模块
加载失败(-70089)。详见仓库 `docs/deployment/quickstart.md` 的 DM 注记与
`docs/deployment/win10-offline-bundle.md`。

## 安全注意

- **备份 master key**:`home/config/.secret_master.key` 是所有数据源密码的加密根。
  丢失即无法解密已存的数据源凭据。请离线安全备份。**它不在包内,不进 git。**
- 管理员密码、数据源密码只以哈希 / Fernet 密文存于数据库,不落明文文件。
- 整个部署包内**零密钥零密码**;所有 secret 都在本机首次运行时生成/输入。
