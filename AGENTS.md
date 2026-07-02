# AGENTS.md — Codex 入口(自动加载的宪法 + 速记)

> 本文件由 agent 启动自动加载(Codex 读 `AGENTS.md`,Claude Code 读 `CLAUDE.md`),
> **直接当宪法用,不再依赖外部 CORE 文件**。
> 完整规则定义在 `AGENT_CONTRACT_v2.md`(开工前必读)。
> 历史 changelog / 踩坑案例 / 设计背景在 `docs/agent-playbook.md`,按需查。
>
> ★ **`AGENTS.md` 与 `CLAUDE.md` 必须保持一字一致**(除首行标题),
> CI `Agents Doc Sync` job 强制 diff,不一致则红。

---

## 1. 行为准则(Karpathy 4 条)

1. **别臆造,先问** — 不确定的需求 / 接口 / 数据形态,先问人或读源码。
   不要"我觉得应该是这样"自我说服。设计稿没写的,见 contract §0 #5。
2. **简单优先,反过度设计** — 能 50 行解决就不写 200 行;能复用现有就不新建。
3. **外科手术改动** — 改一处,不顺手"清理"无关代码;不擅自重构。
   diff 越小越易 review。范围纪律见 contract §0 #4。
4. **目标驱动** — 每行代码要能回答"为了哪个 §5 验收点 / 用户行为存在"。

---

## 2. 红线速记(R1–R10)

> 完整定义 + CI 拦截方式 见 **`AGENT_CONTRACT_v2.md §4`**。改红线相关代码前必读全文。
> **R7 于 2.4.0 Workflow 域生效**(R7 单测已在 CI 激活);R1–R6 / R8–R10 全部 CI 已生效。

| ID | 一行速记 |
|---|---|
| R1 | `app/services/` 禁止 import 数据库驱动 |
| R2 | 业务代码禁止持有明文密码,只能持 `SecretRef` |
| R3 | `Fernet`/`bcrypt` 只允许在 `secretstore/`;`hazmat` 限 `secretstore/`+`license/`;非 secret 指纹用途的 `hashlib`(sql_hash 等)不受限 |
| R4 | PG 连接密码禁止写入 `secret_refs` 表(否则启动循环)|
| R5 | 日志禁止出现敏感值;structlog 脱敏 processor 强制;禁 stdlib logging |
| R6 | `ResultSet` 禁止持有 `DbCursor` |
| R7 | Workflow 节点必须在 `ALLOWED_WORKFLOW_NODE_KINDS` 白名单内 **(2.4.0 Workflow 域生效;R7 单测已在 CI 激活)** |
| R8 | 配置文件禁止明文 password / token / secret;gitleaks 全仓扫 |
| R9 | CI 不跑 SQLite backend;2.0 只支持 PG |
| R10 | 禁 f-string 拼 `uuid4().hex` / `str(uuid4())` |

---

## 3. 凭据 / 服务器信息安全(contract §5 速记)

部署模式二:云服务器 + 专用受限部署账号。**三条铁律**:

- **绝对不进 git** —— 服务器 IP / SSH key / 账号 / 密码 / PG 凭据 / admin 凭据 
  一条不进任何仓库文件(代码 / 配置 / 文档 / commit / 注释 / 临时脚本)
- **只读运行环境变量** —— `DEPLOY_HOST` / `DEPLOY_USER` / `DEPLOY_SSH_KEY_PATH`;
  脚本只用占位 `$DEPLOY_HOST`,**禁止内联真实值**
- **commit 前自查,不靠 gitleaks 兜底** —— 跑一遍 grep 确认无 IP / 账号 / 路径混入

部署操作:**先告人"准备执行什么"**。**删除 / 覆盖既有内容必须先问**。

---

## 4. 前端"跑起来"的最低验证标准

**★ build 通过 ≠ 跑起来通过。**
**★ curl 200 ≠ 前端真渲染** —— curl 200 只证明 SPA index.html 在,
JS 可能崩在 `main.ts` 顶层副作用(典型:Pinia 未注册时 store 被调用 → 白屏)。

声明"前端跑起来了"必须同时满足:

1. **浏览器实际打开页面**
2. **F12 Console 无红色 error**(module evaluation / Pinia / 组件 render 全清)
3. **看到正确首屏 DOM** —— 不是白屏,不是 Vite 错误覆盖层
4. **关键路径手动点过**

### 接后端的页面追加:接口要真走通

**★ 接后端的页面**(登录 / 列表 / 写入 / 查询结果),除上述 4 条还必须:

- 用**真后端**(launcher 起的 API + worker + PG,或通过 SSH 隧道连真服务器),
  **不是 mock,不是"假设接口长这样"**
- **真走通流程**:登录真返回 token、列表真返回数据、查询真返回 result rows、
  错误真返回 4xx/5xx
- **贴真走通的证据** —— 响应体片段 / 网络面板截图 / 完整 curl + stdout
- 后端没起 / 没接通就声明"前端 OK",等于零证据

报告时贴**具体证据**(console / 网络 / DOM / curl)。**不写"应该没问题 / 估计能跑"**。

---

## 5. 提交 / 部署边界

- 任何代码改动 → **feat 分支 → PR → 人 review → 人点 squash**
- **不直接 push main —— 没有例外**(纯文档 / backlog / changelog 改动也走 PR;
  "什么算纯文档"有模糊地带,留口子 = 给绕过 PR 开门)
- **Agent 不自己点 merge**
- **不可逆操作必须先问** —— `git reset --hard` / `rm -rf` / `DROP TABLE` / 
  改防火墙 / 改 systemd / 撤销公网暴露
- 所有 main commit 必须有 `Co-Authored-By: ...`,凭据为零

---

## 6. 启动期 checklist(开工前自己问 4 句)

| 问题 | 不通过的动作 |
|---|---|
| 我要改的是哪条红线相关? | 速记里有 → 读 contract §4 完整定义 |
| 我要新建文件 / 拆模块? | 看 contract §2 目录结构 |
| 我要 commit / push? | 跑凭据 grep,跑 `make check-redlines` |
| 我改了前端,要说"跑起来了"? | 浏览器 §4 4+1 条全过,贴证据 |

---

## 7. 改本文件的规则(防漂移)

- 新规则 / 改规则 → **先改 `AGENT_CONTRACT_v2.md`** 对应章节,再改本文件速记
- 本文件永远是 contract 的**子集 + 索引**,不能出现 contract 里没有的新规则
- 历史 changelog / 踩坑案例 / 设计背景 → 进 `docs/agent-playbook.md`,**不进本文件**
- **`AGENTS.md` / `CLAUDE.md` 改一个必须同步改另一个**(只首行标题不同)。
  CI `Agents Doc Sync` job 强制 diff,不一致则红
