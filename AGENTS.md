# AGENTS.md — Codex 入口

本仓库 agent 行为规则:

1. **首先读 `AGENT_CORE.md`**(已自动加载) —— 这是宪法,30 秒看完
2. **开工前读 `AGENT_CONTRACT_v2.md`** —— 详细法典,任何不确定回这里查
3. **历史 changelog / 踩坑案例 / 设计背景** 在 `docs/agent-playbook.md`,按需查

## 不许触碰的硬底线

- 任何真实**服务器 IP / SSH key / 账号 / 密码 / PG 凭据 / admin 凭据**
  一条不进本仓库任何文件,任何提交
- 配置文件 / 注释 / commit message / 临时脚本 都不能 echo 出凭据
- 边界完整定义见 `AGENT_CONTRACT_v2.md §5`(部署 / 凭据约定)

## 不许做的动作

- **不直接 push main —— 没有例外**(纯文档 / backlog 改动也走 PR)
- **Agent 不自己点 merge** —— 合并是人最后的把关闸
- 任何改动走 feat 分支 → PR → 人 review + 人点 squash
- 不可逆服务器操作(`rm -rf` / `DROP TABLE` / 改防火墙 / 改 systemd)
  **先问人,获明确确认再做**

## 前端"跑起来"的最低标准(见 CORE §4)

- build 通过 / curl 200 / `npm run dev` 启动成功 都**不够**
- 必须浏览器实际打开 + Console 无红 error + 看到首屏 + 关键路径手点过
- **接后端的页面**还要真连后端走通(响应体证据,不是 mock)
- 报告时贴具体证据,不写"应该没问题"
