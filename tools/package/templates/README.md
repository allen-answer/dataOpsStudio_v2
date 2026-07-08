# DataOpsStudio 2.0 — Win10 x64 全离线部署包

本包为**完全离线**部署包:拷到没有网络的 Windows 10 x64 机器上解压即可运行,
无需预装 Python / PostgreSQL / Node,无需联网下载任何依赖。

## 系统要求

- Windows 10 x64(或更高)
- 约 2GB 可用磁盘(解压后含内置 PostgreSQL 数据目录)
- 无需管理员权限;**不要用 root/系统管理员账户跑**(initdb 拒绝)

## 目录结构

```
dataops-studio-<ver>-win10-x64-offline/
  start.bat            首次初始化 + 启动(API + worker + 内置 PostgreSQL)
  stop.bat             优雅停止(活跃查询会先跑完)
  app/                 应用源码
  frontend/dist/       前端 SPA(由 API 进程同端口伺服)
  pgsql/               内置 PostgreSQL 16 服务端(bin/lib/share)
  runtime/python/      内置 Python 3.12(可重定位)
  runtime/uv/uv.exe    进程管理(离线,仅执行本机 .venv)
  runtime/wheels/      全量依赖 wheel(含各数据库驱动)
  .venv/               首次运行离线创建(不在包内)
  home/                运行时实例数据:config/ data/ logs/(首次运行创建)
```

## 首次初始化并启动

双击 `start.bat`(或在 cmd 里运行)。首次运行会自动:

1. 用内置 Python 离线创建 `.venv` 并安装全部依赖 wheel;
2. 生成 bootstrap 密钥(master key / PG 密码 / JWT);
3. 初始化并启动内置 PostgreSQL;
4. 执行数据库迁移到最新版本;
5. 创建管理员账户 —— **会提示你输入管理员密码**(密码只写入数据库 bcrypt 哈希,
   不落任何文件);
6. 启动 API + worker。

启动后浏览器打开 `http://127.0.0.1:8020/` 即为完整前端 + 后端。

### 可配置项(启动前设环境变量)

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATAOPS_API_PORT` | `8020` | API/前端端口 |
| `DATAOPS_PG_PORT` | `15432` | 内置 PostgreSQL 端口 |
| `DATAOPS_HOME` | `<包>\home` | 实例数据目录(config/data/logs) |
| `DATAOPS_ADMIN_USER` | `admin` | 首次创建的管理员用户名 |
| `DATAOPS_ADMIN_PASSWORD` | (交互输入) | 设置后非交互创建管理员(自动化用) |

### 路径注意

解压路径 + `DATAOPS_HOME` 不要太深:内置 PostgreSQL 的本地 socket 路径
(`<DATAOPS_HOME>\data\pg-socket\...`)有 107 字节上限,路径过长会导致 PG
启动失败(pg.log 报 Unix socket 路径过长)。建议解压到较短路径,如 `D:\dataops\`。

## 停止

运行 `stop.bat`。优雅停止会等 worker 当前任务跑完;紧急停止用 `stop.bat --force`。

## 升级

用新版离线包替换 `app/`、`frontend/dist/`、`runtime/wheels/`、`runtime/requirements-frozen.txt`,
删除旧 `.venv/`(下次 `start.bat` 会离线重建),保留 `home/`(实例数据),
再运行 `start.bat`(会自动跑新迁移)。升级前请先备份 `home/`。

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
