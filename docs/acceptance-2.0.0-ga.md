# DataOpsStudio 2.0.0 GA — 回归走查 + 安全自审对照表

> 执行:review agent + 人(2026-06-11)。基线:`2.0.0-skeleton` tag 之后的
> Wave 4(Part B 补全,#38–#44)+ Wave 5(GA 加固,#46–#51)。
> 真机环境:daily-server(`e9a9e81`,migration 0001–0008 全量)。凭据零出现
> (TOTP seed / 临时用户密码全程只存在于服务器端 shell 变量,用毕即毁)。

## GA 回归走查(真机,10/10)

| # | 项 | 证据 | 结论 |
|---|---|---|---|
| 1 | 登录(pyjwt 路径,#49 替换后) | 真 token,后续全链路鉴权正常 | ✅ |
| 2 | MFA enroll + verify(pyotp 路径) | 服务器端临时用户全流程 | ✅ |
| 3 | **激活码立即重放登录被拒**(#50 review 缺口修复) | 401 invalid_mfa_code | ✅ |
| 4 | 新窗口 TOTP 码登录放行 | 200 | ✅ |
| 5 | **同码二次登录被拒**(重放防护主路径) | 401 | ✅ |
| 6 | force-logout(#39) | 200,目标用户会话吊销 | ✅ |
| 7 | 走查产物清理(临时用户/密码文件) | 已删 | ✅ |
| 8 | **SQL job 连接失败细分**(#51 主交付) | 不可达库 → `failed connection_failed`(此前为 sql_failed) | ✅ |
| 9 | SELECT 全链路无回归 | acceptance-mysql 25 行 count 正确 | ✅ |
| 10 | AI 测试端点结构化错误 | off 态 → `ok:false, error:ai_disabled` | ✅ |

另:Wave 4 走查(#42 评论)已覆盖 §6/§9 页面级全流程(真手机验证器);
CI 十 job 含 Compose form / Cold-start / E2E / MySQL / PG Queue hard evidence 常绿。

## 安全自审对照表(backlog 安全项逐条)

| 项 | 状态 | 依据 |
|---|---|---|
| 手写 JWT → pyjwt(alg 白名单,拒 alg=none) | ✅ 已换 | #49 + legacy token 兼容测试 |
| 手写 TOTP → pyotp | ✅ 已换 | #49 |
| TOTP 同窗重放 | ✅ 已防 | #50(四入口:登录/激活/disable/regenerate;FOR UPDATE 串行化)+ 本表 #3/#5 真机证据 |
| frozen ApiError → 事务内 4xx 变 500 | ✅ 已修 | #43/#44 + contextmanager 回归测试 |
| AI L4 出站形态锁(ADR-0016) | ✅ 已锁 | #41(schema le=3 + opt-in 不可关)真机 UI 灰显验证(#42 评论) |
| API key / 密码零回显 | ✅ | #41/#32 评审记录 + 走查抓包 |
| token 吊销(force-logout / revoked_tokens) | ✅ | #39(热路径纯读修正后) |
| R1–R10 红线 + gitleaks | ✅ 常绿 | CI(R3 条文已对齐执行,#46) |
| 密码进 request body 改造 | ⏸ 决策 C 推迟 | 触发条件=外部安全审计(#46 落档) |
| 正式 license 签发 | ⏸ trial-only 发布 | 触发条件=首个商业交付(#46 落档) |
| DM 真实例验证 | ✅ 已完成(GA 后) | 8/8 真实例验证 + 修 3 bug,DM 升 Certified;`docs/acceptance-dm-certified.md`(#53) |

**结论:GA 安全项全部"已闭环"或"已决策落档",无未知敞口。建议发布 2.0.0 GA。**

## 已知遗留(不阻塞 GA,backlog 在档)

- AdapterConnectionError `from None` 吞原始 driver 错误(#51 review 建议,排障可见性)
- F2 worker 独立心跳线程(2.7.0 前)
- Oracle adapter(2.0.x 后续)/ DB2 Preview
