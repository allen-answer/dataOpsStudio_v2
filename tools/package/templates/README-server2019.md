# DataOpsStudio 2.0 — Windows Server 2019 x64 精简离线包

本包是 Win10 offline 包针对**干净的 Windows Server 2019** 裁剪出的版本,启动/停止/升级流程
与下文的 Win10 说明完全一致,只有以下差别:

- **不带桌面壳(没有 `DataOpsStudio.exe`)**:Server 2019 不自带 WebView2。双击 `start.cmd`
  启动,用浏览器访问。
- **自带 VC++ 运行库**:`pgsql\bin` 内置 `vcruntime140.dll` / `vcruntime140_1.dll` /
  `msvcp140.dll`(应用本地部署),目标机**不需要**再装 `vc_redist.x64.exe`。
- **PostgreSQL 只保留服务端与运维工具**:`initdb` / `pg_ctl` / `postgres` 以及
  `psql` / `pg_dump` / `pg_dumpall` / `pg_restore` / `pg_isready` / `pg_controldata` /
  `pg_resetwal`;StackBuilder、ecpg、pgbench、回归测试工具和 `.lib` 开发库已去掉。

## 解压前先解除锁定

zip 若经浏览器 / 聊天工具 / 网盘传到服务器,会带「来自其他计算机」标记,解压出的脚本会被
拦截。解压**前**右键 zip → 属性 → 勾选「解除锁定」,或在 PowerShell 执行:

```powershell
Unblock-File -LiteralPath .\dataops-studio-<ver>-server2019-x64-offline.zip
```

## 解压到短路径

Server 2019 默认没开长路径支持,首次运行装依赖时 DB2 驱动 wheel 里有约 90 字符的内部路径,
解压目录过深会报 `WinError 3 系统找不到指定的路径`。请解压到短路径,包目录全路径
**不超过 100 字符**,例如 `D:\DataOpsStudio\dataops-studio-<ver>-server2019-x64-offline`。

## 用哪个浏览器访问

Server 2019 自带的 IE11 **打不开**本前端。二选一:

1. **在服务器上用**:另装 Edge 或 Chrome,打开 `http://127.0.0.1:8020/`。
2. **从别的电脑访问**:启动前设置监听全部网卡,并只对内网放行端口:

   ```powershell
   $env:DATAOPS_API_HOST = '0.0.0.0'
   .\start.cmd
   ```

   ```powershell
   # 管理员 PowerShell;RemoteAddress 换成实际允许访问的网段
   New-NetFirewallRule -DisplayName 'DataOpsStudio 8020' -Direction Inbound -Protocol TCP `
     -LocalPort 8020 -RemoteAddress 10.0.0.0/8 -Action Allow
   ```

   内置 PostgreSQL 始终只监听 `127.0.0.1`,不需要也不要对外放行 `15432`。

## 账户

可以用 Administrator 登录后运行:Windows 上 `initdb` / `pg_ctl` 会自动降权启动 PostgreSQL。
包内其余说明里的「不要用系统管理员账户跑」针对的是 Linux root。

---
