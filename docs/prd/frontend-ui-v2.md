# DataOps Studio 2.0 — 前端 UI 功能 PRD(完整版)

> **来源**(不编造,从下列真信息提取):
> - `app/api/schemas.py` + `app/api/routes/core.py`(后端真字段)
> - `AGENT_CONTRACT_v2.md §5`(范围)
> - `docs/design/dataops-2.0-tech-design-v0.3.2.md`(1866 行 2.0 设计稿)
> - `docs/legacy/V1_AS_IS.md`(1.x 真实代码扫描)
>
> **视觉方向**(已锁,本文档不动):Spotify+Figma 深色风、禁纯黑纯白、4 变体、双主题。
>
> **结构**:
> - Part A:2.0.0 实建页面(5 个,GA 前必有)
> - Part B:2.0.x 补的页面(MFA / admin,GA 前必有,后端已埋接口)
> - Part C:2.1+ 后置页面(视觉语言锚点,设计系统现在要支持)
> - Part D:跨页公共组件 + 设计系统锚点
> - 附录 A:视觉系统给设计师的清单
> - 附录 B:2.0.0 GA 必有清单(给排期用)

---

## 2.0 设计稿 Release Roadmap(背景)

| 版本 | 周期 | 主题 |
|---|---|---|
| **2.0.0** | 2-2.5 月 | 骨架版,功能 ≤ 1.x 50%,架构完整即可 |
| **2.1.0** | 2.5 月 | SQL Workspace 完整 + AI Assist 第一批(计划解释 / 错误翻译 / SQL 改写) |
| **2.2.0** | 1.5 月 | Compare + AI Assist 第二批(字段映射简易) |
| **2.3.0** | 1.5 月 | Scenario Lab(DSL + materialize + verify + run-all) |
| **2.4.0** | 1.5 月 | Lineage(沿用 1.x LineageReport)+ 资产页 + AI 血缘解释 |
| **2.5.0** | 1 月 | Workflow(Job DAG)+ 调度 + 节点白名单 + 通知 |
| **2.6.0** | 2.5 月 | AI Copilot MVP(C1 Schema-aware SQL + C5 Scenario 脚手架) |
| **2.7.0** | 2.5 月 | 多租户 Hosted + RLS + OIDC + C2/C3/C4/C5 进阶 |

---

# Part A — 2.0.0 实建页面(GA 前必有)

## 1. 登录页 `/login`

### 用户目标
拿到合法的访问凭证,进入主应用。

### 页面区域
- 居中卡片(单一)
- 品牌 logo(渐变签名色)+ 产品名 + 短副标
- 表单(用户名 + 密码 + 提交按钮)
- 错误提示槽位(表单下方,inline,不弹 toast)
- License 状态条(顶部条,仅 IN_GRACE / REPAIR / EXPIRED 时显示)
- 一行品牌 footer

### 字段
| 字段 | 类型 | 必填 | 备注 |
|---|---|---|---|
| 用户名 | 文本 | 是 | autofocus,autocomplete=username |
| 密码 | password | 是 | masked,永不返响应 |

### 操作
- 输入用户名 / 密码 → 回车或点登录 → 进入主应用
- **2.0.0 不做**:忘记密码 / 注册 / 第三方 SSO

### 状态
| 状态 | 表现 |
|---|---|
| 初始 | 空表单,用户名 focus |
| 提交中 | 按钮 disabled + 加载点 |
| 凭证错误(401)| inline:"用户名或密码错误" |
| 服务器错误(5xx)| inline:"服务器内部错误,请稍后重试" |
| 网络异常 | inline:"网络异常,请稍后重试" |
| 会话已过期 | 顶部条:"登录已过期,请重新登录" |
| License IN_GRACE(过期 ≤7 天)| 顶部条琥珀色:"License 即将过期,剩余 N 天" |
| License REPAIR | 顶部条红色:"系统进入维护模式,仅允许 license 更新" |
| 空字段提交 | inline:"请输入用户名 / 密码" |
| 成功 | 跳转到 next 或 /projects |

### 后端交互
- 提交凭据 → 服务端 bcrypt 验证 → 返 token + role
- 错误 status code 映射 i18n,不显示后端 raw message

### 设计稿对齐
- §1.2 on-prem 本地账号 / hosted OIDC(2.7.0)
- §8 License 5 态(VALID / TRIAL / IN_GRACE / EXPIRED / REPAIR)

---

## 2. 项目页 `/projects`

### 用户目标
看到我有权访问的所有项目;点进去进入工作区。登录后的"主菜单"。

### 页面区域
- 顶部品牌栏(logo + 产品名 + 用户菜单)
- 标题"项目" + 计数 + "新建项目"按钮(★ 2.0.0 disabled,T5 admin 后开放)
- 项目卡片网格(3 / 2 / 1 列响应式)
- 用户菜单下拉(详见 Part D §1)

### 字段(每个项目卡)
| 字段 | 显示 |
|---|---|
| name | 卡片标题(粗体)|
| description | 正文(无值显灰字 "(无描述)")|
| id | 底部 monospace 灰字 |

### 操作
- 点击卡片 → 跳 `/projects/:id/datasources`
- "新建项目"(disabled)→ tooltip:"管理员功能,T5 admin 路由后开放"
- 用户菜单 → 切主题 / 切语言 / 退出登录

### 状态
- 加载中 / 空 / 失败 / 有数据(标准四态)
- 空 empty state:"还没有项目,联系管理员创建一个项目开始使用"

### 后端交互
- 拉取当前用户可见项目(owner 或 project_members)
- 缓存 30 秒

### 设计稿对齐
- §10.3 必做:登录 + 用户 + 项目 + datasource CRUD

---

## 3. 数据源页 `/projects/:id/datasources`

### 用户目标
管理项目下所有数据连接,看连接通不通,加新连接。

### 页面区域
- 顶部品牌栏 + 用户菜单
- 左侧窄导航(项目 / **数据源(选中)** / SQL / 任务,4 项)
- 主区域:
  - 面包屑(项目名 + id 前 8 位)
  - 标题 "数据源" + 计数 + 右上"新建数据源"主按钮
  - 数据源表格(致密工作区密度)
  - 新建 modal(点按钮触发)
  - 编辑 modal(每行编辑按钮触发)

### 字段(列表表格列)
| 列 | 字段 | 显示 |
|---|---|---|
| 环境 | environment + environment_verified | environment 标签(unknown 灰 / sandbox 蓝 / staging 琥珀 / **prod 红 + 🔒** verified)|
| 名称 | name + id | 主行 name 粗体;副行 id 前 8 位 mono 灰 |
| 类型 | db_type | 标签徽章(MySQL / PostgreSQL / Oracle / DM / DB2)|
| 主机 | host:port | mono 等宽 |
| 库 | database | mono |
| 创建于 | created_at | 本地时区 yyyy-MM-dd HH:mm |
| 测连结果 | (inline 状态)| 上次测连结果 + 时间 + server version |
| 操作 | — | 测试连接 / 编辑 / 删除 按钮 |

**★ 后端永不返回**:`password` / `password_secret_ref`(R2 红线)

### 字段(新建 modal 表单)
| 区块 | 字段 | 必填 | 默认 | 校验 |
|---|---|---|---|---|
| **基础** | 名称 | 是 | — | 非空 |
| | 数据库类型 | 是 | mysql | 下拉:mysql / postgresql / oracle / dm / db2 |
| | 环境 | 是 | sandbox | 下拉枚举:unknown / sandbox / staging / **prod**(选 prod 时弹二次确认)|
| **连接** | 主机 | 是 | 127.0.0.1 | 非空 |
| | 端口 | 是 | 随 db_type:mysql=3306 / pg=5432 / oracle=1521 / dm=5236 / db2=50000 | 1..65535 |
| | 用户名 | 是 | — | 非空 |
| | 数据库名 | 是 | — | 非空 |
| | 密码 | 是 | — | type=password,提交后立刻加密入库,永不再返 |
| **权限**(8 个 allow_* 开关,折叠面板默认收起)| 允许 SELECT | 默认开 | true | — |
| | 允许 EXPLAIN | 默认关 | false | MySQL 用 |
| | 允许 Oracle PLAN_TABLE | 默认关 | false | Oracle 用 |
| | 允许 DM EXPLAIN | 默认关 | false | DM 用 |
| | 允许 Schema 导入(读 DDL)| 默认关 | false | — |
| | 允许 Schema 写入(建表)| 默认关 | false | Scenario 用 |
| | 允许 Scenario 数据写入 | 默认关 | false | — |
| | 允许自动建对比任务 | 默认关 | false | — |
| **高级**(extra,折叠面板默认收起)| Charset | — | utf8mb4 | MySQL 用 |
| | 连接池大小 | — | 4 | 1..32 |
| | 池闲置回收(秒)| — | 600 | — |
| | 禁用连接池 | — | false | checkbox |
| | DSN / 连接串 | — | — | Oracle/DB2 高级 |

### 字段(编辑 modal)
- 与新建相同,**密码字段默认空**(不展示加密的旧密码)
- 留空 = 不修改密码;填新值 = 替换
- 名称 / 类型改动会失效旧连接池
- prod 环境改动需二次确认

### 操作
- "新建数据源" → 弹 modal → 填表 → "创建" → 关 modal + 刷表
- 每行"编辑" → 弹 modal(密码留空)→ "保存"
- 每行"删除" → 弹确认对话框,**若被 task / workflow 引用则拒绝**(列出引用者)
- 每行"测试连接" → inline 状态变化
- "测试连接"成功后,状态条显示:`✓ MySQL 8.0.32 · 235 ms`
- modal 取消 / ESC / 点遮罩 → 关闭

### 状态
**表格**:加载中 / 空 / 失败 / 有数据
**modal**:空表单 / 提交中 / 字段未填齐 / 服务端拒绝 / 网络异常 / 创建成功
**测试连接 inline**:
| 状态 | 表现 |
|---|---|
| 初始 | 按钮"测试连接" |
| 测试中 | 按钮转圈 + "测试中…" disabled |
| 成功 | 绿点 + "连接成功" + server version + 耗时 |
| 失败 | 红点 + "连接失败" + hover 显示分类原因(认证失败 / 主机不可达 / 超时 / 权限不足),**不暴露 driver raw error** |

### 后端交互
- 进入页面 → 拉项目下数据源列表
- 创建 / 编辑 / 删除 → 同步刷新列表
- 测连接 → worker 实测 → 返 ok + server_version + latency_ms + (failed 时)分类 code

### 设计稿对齐
- §3.2a DatasourceConnInfo(extra: dict[str, Any])
- §4.4 连接池配置(pool_max_size / pool_idle_seconds)
- §4.5 operation_policy(8 个 allow_* 标志)
- §1.2 environment 三档(prod 守护)

---

## 4. SQL 工作台 `/projects/:id/sql`

### 用户目标
对数据源跑 SQL,边拉边看结果,过程可取消。产品主战场。

### 页面区域
- 顶部品牌栏 + 用户菜单
- 左侧窄导航(**SQL 选中**)
- 主区域上下分屏:
  - **上栏:SQL 编辑器区**(工作区密度,可选深色底)
    - 工具条左:数据源下拉(显示名 + db_type 徽章)
    - 工具条中:文件名(2.0.0 固定 `query_1.sql`)+ 资源 profile 显示(small/medium/large)
    - 工具条右:Cancel(运行中显示)+ **Run** 主按钮(`⌘↵`)
    - 编辑器:CodeMirror 6,SQL syntax,行号
  - **下栏:结果区**
    - 状态条:状态点 + 摘要(行数 / 耗时 / 截断)+ 取消按钮(运行中)
    - 结果表格:列头粘顶,monospace 等宽数字,行高 ~26px(**virtual scroll 2.0.0 暂不做,先 LIMIT 100;2.1 完整**)

### 字段(数据源下拉)
- 显示当前项目下所有 datasource(name + db_type 徽章 + environment 标签)
- ★ prod 环境数据源在下拉里**额外红色 🔒 警示**(选中后顶部条提示"prod 环境,只读 SELECT")

### 字段(SQL 编辑器)
- 后端 R6 SQL Guard:**只接受首词 SELECT / WITH**,拒多语句、拒 FOR UPDATE
- 占位提示:"-- 选好数据源后,在这里写 SELECT"

### 字段(结果表格)
| 字段 | 显示 |
|---|---|
| columns[].name | 列头主行 |
| columns[].type | 列头副行(driver 原始类型;**GA 前会标准化**,F4 backlog)|
| columns[].nullable | 列头小标记(可选)|
| columns[].primary_key | 列头主键 🔑 标记(可选)|
| rows[].values | 单元格(mono 等宽,数字右对齐,null 灰字 NULL)|
| loaded_rows | 状态条:"共 N 行" |
| truncated | 状态条:"(已截断)" + tooltip 说明 spool 上限 |

### 字段(状态条 — 运行中)
- 状态点:running 蓝脉冲
- 摘要:"运行中… X.Xs" + (可选)"已加载 N/? 行"(边拉边看)
- 按钮:Cancel

### 字段(状态条 — 完成)
| 状态 | 摘要 |
|---|---|
| success | 绿点 + "N 行 · X.Xs" + (有截断)"已截断" |
| failed | 红点 + 结构化错误码 + "重试"按钮 |
| cancelled | 灰点 + "查询已取消" + "重跑"按钮 |
| timeout | 红点 + "查询超时,请缩小数据范围或拆分查询" |

### 操作
- 选数据源 → 数据源下拉
- 写 SQL → 编辑器
- 点 Run / `⌘↵` → 提交
- 点 Cancel → 中断(软取消)
- 滚动看结果,横向滚动
- 复制单元格 `⌘C`
- **2.0.0 不做**:保存为 console / SQL 历史 / SQL 模板 / 4 格式导出 / 多 tab / 元数据浏览器 / 慢 SQL 诊断 / EXPLAIN / 格式化 / expand-star
  → **全部 2.1.0 SQL Workspace 完整版补**

### 状态
| 状态 | 编辑器 | 结果区 |
|---|---|---|
| 未选数据源 | 灰底 | "选好数据源,写一条 SELECT 开始" |
| 已选,空 SQL | 正常 | 空 |
| 提交后,pending | 锁定 | "等待 worker 调度…" |
| 提交后,running | 锁定 | "运行中… X.Xs"(★ 边拉边看:有 spooled 行就先展示)|
| 完成,success | 解锁 | 表格 + 摘要 |
| 完成,failed | 解锁 | 错误码 + 重试 |
| 完成,cancelled | 解锁 | 取消提示 + 重跑 |
| 完成,timeout | 解锁 | 超时提示 |

### 后端交互
- Run → 提交 SQL → 立刻返 job_id + result_set_id
- 轮询 job 状态(0.5 秒)直到 terminal
- success 后按需拉 result(默认 limit=100,offset=0)
- Cancel → 软取消(worker 安全点轮询)

### 设计稿对齐
- §2.6 完整 ResultSet 模型:spool_max_rows=1M / spool_max_bytes=512MB / cursor_max_hold_seconds=300 / result_ttl_days=7
- §2.6.3 "边拉边看":worker 边 fetchmany 边写 spool,前端轮询 loaded_rows
- §2.6.4 max_active_resultsets_per_console=3,LRU 清理

---

## 5. 任务历史 `/projects/:id/jobs`

### 用户目标
看我最近跑过的任务,失败的看是什么类型;成功的可以重看结果。

### 页面区域
- 顶部品牌栏 + 用户菜单
- 左侧窄导航(**任务 选中**)
- 主区域:
  - 面包屑 + 标题 "任务" + 计数
  - 过滤器条:状态多选 / 类型多选 / 时间范围
  - 任务表格(致密)

### 字段(过滤器)
- 状态多选:pending / running / success / failed / cancelled / timeout
- 类型多选:sql_query / test_connection(★ **2.0.0 实建只这两个**;2.1+ 加 compare_run / lineage_analyze / workflow_run 等共 11 种)
- 时间范围:今天 / 7 天 / 30 天 / 自定义
- 项目自动过滤为当前路由 `:id`

### 字段(列表表格列 — JobListItem 严格映射)
| 列 | 字段 | 显示 |
|---|---|---|
| 状态 | status | 状态点 + 文字(详见 Part D §3 状态点规范)|
| 类型 | kind | 标签徽章 |
| Job ID | id 前 12 位 | mono 等宽,可点击 |
| 错误 | error(结构化 code) | 仅 failed/cancelled/timeout 时显示 |
| 创建于 | created_at | 本地时区 |
| 开始 | started_at | 同上,无值显 `—` |
| 耗时 | finished_at - started_at | 自动单位 ms/s/m |
| 操作 | — | "查看结果" / "重跑"(2.1+) |

**★ 后端永不返回**:payload(含 SQL 原文)/ worker_id / 原始 jobs.error / result_set_id

### 字段(结构化错误码字典)
| code | 友好文案 |
|---|---|
| `cancelled` | 查询已取消 |
| `query_timeout` | 查询超时,请缩小数据范围 |
| `datasource_connection_failed` | 无法连接到数据源 |
| `sql_execution_failed` | 查询执行失败,请检查 SQL 语法 |
| `job_failed` | 任务执行失败 |

### 操作
- 点击行 / "查看结果" → 跳 SQL 工作台,加载该任务结果
- 切过滤器
- **2.0.0 不做**:"重跑"按钮(2.1+)/ 删除任务 / 任务详情 modal

### 状态
- 加载中 / 空(分有 filter / 无 filter)/ 失败 / 有数据
- 空(无 filter):"还没有任何任务,去 SQL 工作台跑第一条查询" + 跳转链接
- 空(有 filter):"当前过滤条件下没有任务" + "清除过滤"

### 后端交互
- 拉当前项目下当前用户的任务列表(jobs.owner_user_id 过滤)
- 自动每 5 秒轮询(仅当列表中有 pending/running)

### 设计稿对齐
- §2.5 Job 抽象:11 种 kind 枚举(2.0.0 只实建 sql_query / test_connection)
- R2 结构化错误码:不返原始 DB 错误(已实现)

---

# Part B — 2.0.x 补的页面(GA 前必有)

## 6. 账户安全 `/account/security`(★ MFA)

### 用户目标
开启 / 关闭自己的 MFA,管理 recovery codes,改密码。

### 页面区域
- 顶部品牌栏 + 用户菜单(本页"账户安全"高亮)
- 主区域三个卡片:
  - 密码卡:改密码
  - MFA 卡:开关 / 扫码 / 验证
  - Recovery codes 卡:列已用 / 未用,重新生成

### 字段
- 改密码:旧密码 + 新密码 + 确认
- MFA 开启:扫描 QR(展示 secret 文本备份)+ 输入 6 位 TOTP 验证
- MFA 关闭:输入当前 TOTP 验证才允许关
- Recovery codes:8 个一组,一次性消费,打印 / 复制 / 下载

### 操作
- 改密码(走 verify-password 二次确认)
- 开 / 关 MFA
- 重新生成 recovery codes(销毁旧的)
- 下载 codes 为 txt

### 状态
- MFA 未开 / 已开
- codes 未生成 / 已生成 / 已部分使用(显示已用 N/8)

### 版本归属
**2.0.x(GA 前必有)**;`SecretKind.MFA_TOTP_SEED` 在设计稿 §7.3 已留 secret 类型位

---

## 7. 用户管理 `/admin/users`(★ admin only)

### 用户目标
admin 增删改用户、改角色、重置密码、强制下线。

### 页面区域
- 顶部品牌栏 + admin 顶部条(深色识别 admin 路由)
- 左侧 admin 导航(用户 / 项目 / AI 配置 / 审计 / 调度监控 / Aspect 治理)
- 主区域:标题 + "新建用户" + 用户表格 + 编辑 modal

### 字段(列表)
| 列 | 字段 | 显示 |
|---|---|---|
| 用户名 | username | 主行 |
| 角色 | role | 标签:admin / editor / viewer |
| MFA | mfa_enabled | ✓ / ✗ |
| 创建于 | created_at | — |
| 最后活跃 | last_seen_at | 相对时间 |
| 操作 | — | 编辑 / 重置密码 / 强制下线 / 删除 |

### 字段(新建 / 编辑 modal)
- 用户名(≤64)
- 角色下拉
- 初始密码(新建时;编辑时 disabled,有专门"重置密码")
- 显示名(可选)

### 操作
- 增删改用户
- 改角色(降级 admin 要二次确认)
- 重置密码(生成临时密码,要求首次登录改)
- 强制下线(吊销 JWT)
- 关闭用户 MFA(忘了 recovery codes 的兜底)
- 删除用户(检查是否还是项目 owner)

### 版本归属
**2.0.x(GA 前必有,T5 license + admin 路由后)**

---

## 8. 项目管理 `/admin/projects`(★ admin only)

### 用户目标
admin 创建项目、改 owner / 成员、删除项目(级联检查)。

### 页面区域
- admin 导航 + admin 顶部条
- 主区域:标题 + "新建项目" + 项目表格

### 字段
| 列 | 字段 |
|---|---|
| 名称 | name(≤64)|
| owner | owner_user_id(显示 username)|
| 成员数 | members[].length |
| 数据源数 | (count from datasources where project_id) |
| 任务数 | (count from jobs where project_id) |
| 创建于 | created_at |
| 操作 | 编辑 / 删除 |

### 字段(编辑 modal)
- name / description / owner / members 多选

### 操作
- 增删改 / 改 owner / 增删 members
- 删除项目要二次确认(列出级联影响:N 个数据源、M 个任务会一并删除或归档)

### 版本归属
**2.0.x(T5 admin 后)**

---

## 9. AI 配置 `/admin/ai-config`(★ admin only)

### 用户目标
admin 配 LLM provider / key / model,测试连接,监控用量。

### 页面区域
- admin 导航 + admin 顶部条
- 主区域:provider 配置卡 / 测试连接 / 数据出站策略卡 / 用量统计(2.6+)

### 字段(provider 配置)
- provider 下拉:`mock` / `off` / `openai_compatible` / `anthropic` / `ollama`
- model(字符串)
- API key(password 类型,提交后 Fernet 加密入库)
- base_url(可选)
- enable_inference / enable_auto_translation 开关
- API key 来源标记:`stored`(本仓库)/ `env`(`OPENAI_API_KEY` fallback)

### 字段(数据出站策略 — §2.7 + ADR-0016)
- 数据敏感度分级:**L1**(系统/元数据,默认可出)→ **L2**(SQL 文本)→ **L3**(脱敏后的样本)→ **L4**(原始样本值,**默认不出**)
- 每级对应 toggle(on-prem 默认 L1 开 / L2-L4 关;hosted 默认 L1-L2 开 / L3-L4 关)
- 部署形态自动锁定下限(on-prem L4 永远不能开)

### 操作
- 改 provider / model / key
- 点"测试连接"(走 AI Gateway test endpoint)
- 调出站策略

### 状态
- provider=off:其它字段 disabled
- 测试连接:测试中 / 成功(显示延迟 + token 余额)/ 失败

### 版本归属
- **2.0.0 后端 AI Gateway 壳已埋**(§2.7)
- **2.0.x admin UI 出**;**2.6.0 Copilot 上线前必有**

---

## 10. 审计日志 `/admin/audit-logs`(★ admin only)

### 用户目标
admin 查 mutating 行为流水(谁在什么时候改了什么)。

### 页面区域
- admin 导航 + admin 顶部条
- 主区域:过滤器条 + 日志表格(virtual scroll,大数据量)

### 字段(列表)
| 列 | 字段 | 显示 |
|---|---|---|
| 时间 | created_at | yyyy-MM-dd HH:mm:ss.SSS |
| 用户 | actor(username)| — |
| 动作 | action | 标签徽章(create / update / delete / login / logout / reveal_secret 等)|
| 对象 | object_type + object_id | "datasource:abc12345" |
| 结果 | outcome | success / failed / denied,带状态点 |
| 详情 | detail(JSON)| 点击展开 |
| Request ID | request_id | mono,可复制(关联 Prometheus / structlog)|

### 字段(过滤器)
- 时间范围 / 用户 / 动作类型 / 结果 / 对象类型

### 操作
- 切过滤器
- 点行 → 展开 JSON 详情
- 导出 CSV(选中范围)

### 版本归属
- **2.0.0 后端写入**(§9 三管线日志 + audit_logs 表)
- **2.0.x admin UI 出**(GA 前必有)

---

# Part C — 2.1+ 后置页面(视觉语言锚点)

## 11. SQL 工作台完整版(★ 2.1.0)

**vs 2.0.0 加什么**:

### 11.1 多 Console 管理
- 左侧侧栏:我的 console 列表(Postman collection 风格)
- 新建 / 重命名 / 删除 / 钉选 / 切换 console
- 每个 console:独立 SQL + 独立结果区

### 11.2 SQL 历史
- 顶部 tab 切"历史" → 时间倒序列我跑过的 SQL(去重 + 最近 N 条)
- 点一行 → SQL 灌回当前 console 编辑器
- 过滤:按数据源 / 按时间

### 11.3 SQL 模板(SqlTemplate)
- 左侧 tab "模板":分类列模板
- 字段:name / description / sql_text / variables(`{{name}}` 占位)/ category / project_id
- 用户操作:浏览 / 搜索 / 点击插入到编辑器(变量值面板提示)
- admin 操作:模板 CRUD

### 11.4 元数据浏览器
- 左侧第三个 tab:数据源 → schemas → tables / views → columns 树
- 点击 schema / table → 展开列
- 点击 table → 自动 `SELECT * FROM <table> LIMIT 100` 写到编辑器
- 每列显示:name / type / nullable / 主键标 / comment
- 缓存:per-datasource 7 天

### 11.5 SQL 工具
- 格式化按钮(`POST /format`)
- expand-star(把 `SELECT *` 自动展开成全列)
- EXPLAIN 按钮(MySQL EXPLAIN / Oracle PLAN_TABLE)
- 结果区切 tab:Result / Plan / Stats

### 11.6 4 格式导出
- 结果区右上"导出"按钮 → 弹格式选择:CSV / Excel / JSON / SQL(INSERT 语句)
- 异步导出 → 进度条 → 下载令牌(一次性)
- 公式注入防御(`=` `+` `-` `@` 开头加 `'` 前缀)

### 11.7 慢 SQL 诊断 / SqlDiagnosisView
- 主菜单"慢 SQL 诊断"独立页(也可以从 SQL 工作台一键跳转)
- 输入 SQL → 拿 EXPLAIN plan + 规则匹配 → 问题清单
- 规则数:MySQL 4 / Oracle 6 / DM 同 Oracle
- 问题分级:warning / critical
- "用 AI 优化"按钮 → 走 AI Assist → LLM 改写 SQL + 给 DDL 建议
- Plan 历史 + plan diff(两次 EXPLAIN 比对)

### AI Assist 嵌入(2.1)
- SQL 出错时 → "AI 翻译错误"按钮(英文 DB 错 → 中文友好)
- SQL 编辑器右键 → "AI 解释这段 SQL" / "AI 改写"
- EXPLAIN 结果旁 → "AI 解释执行计划"

### 视觉锚点要求
- **多 tab 形态**(console / 历史 / 模板 / 元数据 4 tab + 内容区 console 多 tab)
- **左侧第二级树**(元数据浏览器)— 设计系统要支持
- **状态点用在 SQL 历史每行**
- **AI 入口图标统一**(可能用 sparkles 渐变,3 处出现)

---

## 12. 数据对比 Compare(★ 2.2.0 — 视觉核心)

**vs 我之前 PRD 的更正:**
- **不是三色,是四桶**:only_source / only_target / diff / same
- **5 种 source kind**(SQL / Excel / CSV / Parquet / Excel form)
- 任务可保存(CompareTask)

### 12.1 主页面 `/projects/:id/compare`
- 任务列表(左)+ 编辑区(右)
- 任务卡:name / source-target / 上次运行时间 / 状态点

### 12.2 任务编辑 8 步骤
1. 选 source(数据源 / 文件)
2. 选 target(数据源 / 文件)
3. SQL 编辑(double 模式时两侧独立)
4. 主键映射(多列复合,左右列对齐 UI)
5. 列映射(列名不同时手动连)
6. 比较列选择(勾掉 ignore_columns)
7. 规则配置(CompareRules + RunLimits)
8. 跑

### 12.3 CompareRules 字段
- ignore_columns / column_mappings / numeric_tolerance / trim_strings / case_insensitive / empty_as_null / schema_policy(warn|strict)

### 12.4 RunLimits 字段
- max_rows / export_max_rows / fetch_chunk_size / stream_compare / result_format / persist_same_bucket / query_timeout_seconds / run_disk_quota_mb

### 12.5 结果区 4 tab(核心视觉)
- **only_source**(左有右无,**Deleted,红**)
- **only_target**(右有左无,**Added,绿**)
- **diff**(同主键字段差异,**Changed,琥珀**)
- **same**(完全相同,**灰,默认不落盘**)
- 每 tab 标签带计数:`增 12 · 删 5 · 改 8 · 同 2034`

### 12.6 diff tab 单元格分裂
- 同主键有差异时,每差异单元格"分裂"成两行:
  - 旧值(红删除线)
  - 新值(绿)
- 无变化的单元格单行普通显示
- 主键列固定左侧不滚动

### 12.7 操作
- "开始对比" → 异步跑(走 Job kind=compare_run)
- 切 4 tab
- 每行"复制为 SQL"(生成 INSERT / DELETE / UPDATE)
- "导出 Excel"(异步,4 桶分 sheet,列差异高亮)
- 取消运行
- "保存为模板" / "从模板新建"

### 12.8 AI Assist(2.2.0 新)
- 列映射阶段:"AI 推荐列映射"按钮(基于列名相似度 + 业务样本)

### 视觉锚点(★ 必须现在备好)
- **4 色 token**(增 emerald / 删 red / 改 amber / 同 slate)
- 主色 + 浅底 + 深色提亮一档
- DBA 看 8 小时不刺眼(饱和度克制)
- 主键列高亮视觉
- 单元格分裂版式(垂直堆叠 / 水平箭头 → 两种都画出来选)
- 4 tab 徽章式计数

---

## 13. 场景 Scenario Lab(★ 2.3.0)

### 13.1 主页面 `/projects/:id/scenarios`
- 列我的 scenarios + 模板
- 卡片:name / 表数 / 行数估算 / 上次跑时间 + 操作

### 13.2 Scenario 编辑器
- YAML 编辑 + 表单可视化(两栏切换)
- 表定义:列(generator)+ 索引 + anomaly 注入

### 13.3 7 种 column generator
- `uuid_short` / `random_int(+zipf 分布)` / `realistic`(AI 业务样本)/ `timestamp` / `enum(+weights)` / `constant` / `sequence(+prefix)`

### 13.4 6 类 anomaly
- `missing_rows` / `extra_rows` / `value_drift` / `null_drift` / `duplicate_pk` / `type_mismatch`

### 13.5 工作流
- 选数据源 → "Materialize" 建表 + 灌数据
- "AI 填充" → realistic 列 LLM 生成业务样本池
- "Verify" → actual vs yml expected,5 状态(pass / fail / no_expected / no_task / no_run)
- "Run all" → 整套(materialize + AI fill + 建 compare task + 跑 + verify)

### 13.6 ★ 设计稿 §10.3.1 跨版本约束
- **Scenario 模板数据结构 2.3.0 就要预留"供 AI 读真实数据分布"字段** → 服务 2.6.0 C5 Scenario 脚手架
- 设计系统要支持"AI 上下文输入"组件

### 视觉锚点
- 表单 / YAML 切换
- generator 卡片选择器
- anomaly 注入开关条
- AI 填充进度条

---

## 14. 血缘 Lineage(★ 2.4.0)

### 14.1 设计稿 §2.4 核心策略(v0.3.2 修正)
- 沿用 1.x LineageReport(20 字段,**不收敛**)
- 2.4.0 先做核心 + 后续做复杂

### 14.2 LineageReport 20 字段分层
- **表/列层**:tables / columns / insert_mappings / aliases
- **写操作层**:target_summary(含 refresh_mode)/ table_roles
- **SQL 子句**:joins / filters / group_by / unions
- **动态 SQL / 存储过程**:dynamic_sql_count / dynamic_sql_segments / procedure_segments
- **变量**:variables(PL/SQL)
- **图**:graph_edges / graph_groups
- **警告**:parse_errors / warnings
- **详情**:statements / semantic_lineage / report
- **AI 字段**:ai_enrichment / ai_inferred

### 14.3 2.4.0 第一版做的部分
- 表级血缘(graph_edges + tables)
- 列级血缘(insert_mappings + columns)
- 解析错误(parse_errors)

### 14.4 2.4.x 后续
- 存储过程深度解析(procedure_segments)
- 动态 SQL 推断(dynamic_sql_*)
- PL/SQL 变量(variables)
- AI 推断(ai_inferred)

### 14.5 页面
- **LineageWorkbenchView**(单脚本)— 粘 SQL → 看血缘
- **LineageView**(批量)— 上传 .sql / .txt / .zip → 批量分析
- **LineageReportView**(详细报告)— 20 字段分组展示
- **AssetDetailView**(单资产)— 资产 + aspect + 上下游

### 14.6 图引擎(2.4.0 必有)
- 表节点 + 列节点 + 边
- 缩放 / 平移 / 聚焦
- 业务分组高亮(grouping rules)
- 解析错误节点标红警

### 14.7 AI Assist(2.4.0 新)
- "AI 解释这段血缘"(自然语言总结)
- "AI 推断动态 SQL"(默认关,admin 开)
- "AI 补 column attribution"(默认关)

### 14.8 Trace-compare
- 选两次 lineage 跑结果 → 看 diff(回归)

### 视觉锚点
- 图节点 / 边 / 高亮
- 20 字段 envelope 设计要能撑(分组卡片 + 折叠)
- "AI 推断"标志(区分确定结果 vs AI 推断)

---

## 15. 资产 / Aspect 治理(★ 2.4.0,跟 lineage 一起)

### 15.1 资产详情 `/assets/:id`
- 来自 schema 导入或 lineage 自动发现
- 显示:asset name / type(table)/ 数据源 / 列清单 / aspect 列表 / 上下游血缘

### 15.2 6 类 aspect
- `owner`(负责人 / 团队)
- `pii`(包含 PII 等级:none / yellow / red)
- `sla`(更新时长 / 重要程度)
- `sensitive`(机密分级)
- `tag`(自由标签)
- `business_term`(业务术语)

### 15.3 Aspect 操作
- 用户视角:看 aspect / 编辑 aspect 值
- admin:配 aspect schema(`asset_aspects.yml`)

### 15.4 Aspect History
- 每次变更 append-only 历史
- 显示:谁 / 何时 / 改了什么 / 备注

### 15.5 Schema 导入
- 从数据源 introspect → 批量建资产
- DDL 上传 → 批量建资产

### 15.6 AspectGovernanceView(admin)
- admin 改 aspect schema(增删类型,改字段)
- 改 grouping rules

### 视觉锚点
- 资产卡:多 aspect 标签陈列
- PII 红 / yellow 视觉(用标签 token)

---

## 16. 工作流 Workflow(★ 2.5.0)

### 16.1 设计稿 §2.8
- DAG of Jobs,节点类型严格白名单(R7 红线)
- `ALLOWED_WORKFLOW_NODE_KINDS`:`sql_query` / `sql_explain` / `compare_run` / `scenario_materialize` / `scenario_run_all` / `lineage_analyze` / `export_excel` / `notify` / `sleep` / `branch`
- **★ 1.x 的 `http` 任意 URL 节点在 2.5.0 不做**(R7 拒)

### 16.2 页面
- WorkflowListView — 我的工作流 + 模板
- WorkflowDetailView — DAG 编辑器
- WorkflowRunView — 看单次跑的逐节点状态
- WorkflowTemplateView — 模板管理

### 16.3 DAG 编辑器
- 节点 palette(只显示白名单)
- 拖拽节点到画布
- 连接节点(depends_on)
- 节点配置面板(右侧)
- 变量插值:`${name}` / `${nodes.X.Y}` / `${var | sql_in}` 过滤器

### 16.4 调度 / Sensors
- 工作流配 `schedule_cron`(每天 / 每周等)
- Sensor:file(监听文件)/ workflow_success(链式)

### 16.5 SchedulerMonitorView(admin)
- 下次触发时间 / 上次结果 / job 详情

### 视觉锚点
- DAG 节点形态(每种 kind 不同颜色 / 形状)
- 节点状态点(同 Job 状态点)
- 白名单 vs 自定义节点区分(防误以为能接任意 URL)
- 调度时间轴

---

## 17. AI Copilot 横切面板(★ 2.6.0)

### 17.1 设计稿 §2.7.1 + ADR-0010 + ADR-0011
- AI 三层:Gateway(2.0.0)→ Assist(2.1+)→ **Copilot**(2.6.0)
- Copilot = 多步推理 + tool calling + memory
- 横切面板,**所有能力域可用**(SQL / Compare / Scenario / Lineage)

### 17.2 页面 / 组件
- 全局浮窗(右下)或侧边抽屉(右侧)
- 历史对话(Memory)
- 工具调用日志(可展开看 LLM 用了什么 tool)
- 数据敏感度提示(每次出站标 L1-L4)

### 17.3 2.6.0 MVP demo
- **C1 Schema-aware SQL**:基于数据源 schema 帮你写 SQL("查近 7 天活跃用户")
- **C5 Scenario 脚手架(基础)**:基于已有表生成 scenario YAML 模板

### 17.4 2.7.0 进阶
- **C2 / C3 / C4 / C5 进阶**

### 视觉锚点(★ 现在备好)
- Copilot 入口图标 + 渐变(渐变签名色专门给 AI 用)
- 数据敏感度小标签(L1 灰 / L2 蓝 / L3 琥珀 / L4 红 disabled)
- "AI 推理中"状态(类似 ChatGPT 三点流动)
- "工具调用"可视化(子任务卡片)

---

## 18. 多租户 / Hosted 设置(★ 2.7.0)

- 租户切换器(顶部下拉)
- OIDC / SSO 登录
- RLS 数据隔离
- 多租户用量统计

(2.7.0 才做,设计语言需要支持租户切换器的位置)

---

# Part D — 跨页公共组件 + 设计系统锚点

## D.1 用户菜单(每页右上)

| 项 | 位置 |
|---|---|
| 头像(渐变签名色)+ 用户名前 8 位 + 角色徽章 | 入口 |
| 用户 ID 全文 | 下拉首行,mono |
| 角色 | 下拉次行 |
| 主题切换(浅 / 深) | 下拉中段 |
| 语言切换(中 / 英) | 下拉中段 |
| 账户安全 | 下拉中段(进 §6 MFA 页) |
| **admin only**:管理后台入口 | 下拉中段(灰色提示) |
| 退出登录 | 下拉底部,红字 |

## D.2 左侧窄导航(Nav Rail)

| 项 | 显示条件 |
|---|---|
| 项目 | 始终显示 |
| 数据源 | 在某项目下时(`/projects/:id/...`) |
| SQL | 同上 |
| **Compare** | 同上,**2.2+** |
| **任务** | 同上,2.0.0 名为"任务" |
| **Scenario** | 同上,**2.3+** |
| **Lineage** | 同上,**2.4+** |
| **Workflow** | 同上,**2.5+** |
| 分隔条 | — |
| 全局搜索(顶部) | **2.1+** |
| AI Copilot 入口(底部) | **2.6+**,有渐变签名 |

设计系统要支持"动态增减导航项"(随版本和上下文)

## D.3 状态点规范(全局复用)

| Job 状态 | 颜色 | 动画 |
|---|---|---|
| pending | slate-400(灰) | 静态 |
| running | sky-500(蓝) | pulse-soft + pulse-ring 双层 |
| success | emerald-500(绿) | 静态 |
| failed | red-500(红) | 静态 |
| cancelled | slate-400(灰) | 静态 |
| timeout | red-500 | 静态 |

任务状态 / 数据源测连结果 / scheduler 状态 / workflow 节点状态 全部复用同一组规范。

## D.4 License 状态横条(顶部)

| License Mode | 颜色 | 文案 |
|---|---|---|
| VALID | (不显示) | — |
| TRIAL | sky-500 浅底 | "试用版,剩余 N 天" |
| IN_GRACE | amber-500 浅底 | "License 即将过期,剩余 N 天,请尽快更新" |
| EXPIRED | red-500 浅底 | "License 已过期,系统功能受限" |
| **REPAIR** | red-500 深底 | "★ 系统进入维护模式,仅允许 license 更新" |

REPAIR 模式下:全部写操作 disabled,只允许查看 + 更新 license

## D.5 AI 数据敏感度 4 级标签

| 等级 | 颜色 | 含义 | 默认出站 |
|---|---|---|---|
| L1 | slate-400 | 系统 / 元数据 | on-prem 开 / hosted 开 |
| L2 | sky-500 | SQL 文本 | on-prem 关 / hosted 开 |
| L3 | amber-500 | 脱敏后样本 | 关 |
| L4 | red-500 | 原始样本值 | 永远关(on-prem 锁死) |

AI Copilot 面板每次调用前显示"本次将出站 L? 数据"提示

## D.6 数据库类型徽章(全局)

| db_type | 颜色 | 显示 |
|---|---|---|
| mysql | sky-500 | MySQL |
| postgresql | sky-600 | PostgreSQL |
| oracle | red-500 | Oracle |
| dm | amber-500 | DM |
| db2 | slate-500 | DB2 |

## D.7 Environment 三档(数据源)

| environment | 颜色 + 视觉 | 守护行为 |
|---|---|---|
| unknown | slate-400 灰 | 默认,无守护 |
| sandbox | sky-500 浅蓝 | 提示"测试环境" |
| staging | amber-500 琥珀 | 提示"准生产" |
| **prod** | red-500 红 + 🔒 锁 | **二次确认 + verified 必填** |
| `+ environment_verified=true` | 加 ✓ 标 | admin 显式 verified |

## D.8 Compare 4 桶颜色 token(★ 设计系统必备)

| 桶 | 主色 | 浅底 | 深底提亮 | 用在 |
|---|---|---|---|---|
| only_target(Added)| emerald-500 | emerald-50 | emerald-400 | 桶 tab / 主键单元格高亮 / 整行底色微 |
| only_source(Deleted)| red-500 | red-50 | red-400 | 同上 |
| diff(Changed)| amber-500 | amber-50 | amber-400 | 同上 + 单元格分裂背景 |
| same | slate-400 | slate-50 | slate-700 | 同上 / 通常不展示 |

要求:饱和度克制(看 8 小时不刺眼),在浅 / 深主题下都成立

## D.9 Workflow 节点形态(★ 视觉锚点)

| Kind | 颜色 | 图标 |
|---|---|---|
| sql_query | sky | Terminal |
| sql_explain | sky | FileSearch |
| compare_run | emerald | Diff |
| scenario_materialize | amber | Sparkles |
| scenario_run_all | amber | Play |
| lineage_analyze | violet | GitGraph |
| export_excel | slate | Download |
| notify | sky | Bell |
| sleep | slate | Clock |
| branch | slate | GitBranch |

**★ 白名单外的"自定义"节点不显示**(防误以为能接 HTTP)

---

# 附录 A — 视觉系统给设计师的清单

设计稿要现在交付的 token + 组件:

1. **4 桶差异色**(增/删/改/同,8 小时友好)
2. **6 状态点**(pending/running/success/failed/cancelled/timeout)
3. **5 db_type 徽章**
4. **4 环境徽章** + verified 标
5. **5 License 状态条**(VALID 不显示 + 4 异常态)
6. **4 AI 数据敏感度标签**
7. **10 Workflow 节点形态**(白名单)
8. **AI Copilot 入口**(渐变签名色专属)
9. **AspectGovernance 6 类标签**(owner/pii/sla/sensitive/tag/business_term)
10. **多 tab 切换**(SQL workspace console / Compare 4 桶 / 元数据浏览器)
11. **左侧树**(元数据浏览器)
12. **DAG 图节点 + 边**(Workflow / Lineage)
13. **单元格分裂版式**(Compare diff,垂直/水平 2 方案)
14. **virtual scroll** 大数据表格(SQL 结果 / Compare 结果)
15. **modal**(数据源创建 / 编辑 / 删除确认)
16. **admin 顶部条**(深色识别 admin 路由)

---

# 附录 B — 2.0.0 GA 必有清单(给排期用)

| 项 | 状态 | 备注 |
|---|---|---|
| 登录(基础) | ✓ 已合 | PR #13 |
| 项目列表 | ✓ 已合 | PR #13 |
| 数据源 CRUD + test | ⚠ 当前只新建,要补**编辑 / 删除** | PR β 已开 |
| 数据源 environment 三档 | ✗ 未做 | **GA 前必加** |
| 数据源 allow_* 8 个 | ✗ 未做 | **GA 前必加** |
| 数据源 extra 高级 | ✗ 未做 | 折叠面板 |
| 测连结果含 server_version + latency | ✗ 未做 | 要扩响应字段 |
| SQL 工作台基础 | ✗ 未做 | PR γ 待开 |
| 任务历史基础 | ✗ 未做 | PR δ 待开 |
| MFA + 账户安全 | ✗ 未做 | 2.0.x 中后段 |
| 用户管理 admin | ✗ 未做 | T5 license + admin 路由后 |
| 项目管理 admin | ✗ 未做 | 同上 |
| AI 配置 admin | ✗ 未做 | 2.6 前 |
| 审计日志 admin | ✗ 未做 | GA 前 |
| License 5 态横条 | ✗ 未做 | T5 上线时 |
| AI 数据敏感度提示 | ✗ 未做 | AI 接通时 |

---

## 文档维护规则

- 后端 schema 改了 → 同步改本文档"字段"部分
- 设计稿改了 → 同步改本文档"设计稿对齐"部分
- 1.x 参考改了 → 同步改本文档"vs 1.x"部分
- 任何"编"的字段都要标 ⚠ 或注明"推测" / "未明确"
