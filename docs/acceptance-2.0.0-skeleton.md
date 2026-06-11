# DataOpsStudio 2.0.0 骨架 — contract §5 全量验收报告

> 执行:review agent + 人(2026-06-11)。逐条对照 `AGENT_CONTRACT_v2.md §5` 验收标准。
> 真机环境:daily-server(portable 形态 dogfood 实例,main `d79f6b3`+)+ 同机隔离
> docker compose 栈(PR #35 分支 `2dbc892`)。凭据全程零出现(会话/仓库/日志)。

## 验收标准逐条对照

> contract §5 原文:portable 解压 → launcher 启动(PG+API+worker)→ 登录 → 加一个
> MySQL 数据源(密码经 SecretStore 加密)→ SQL 控制台跑 SELECT(经 worker,结果写
> spool)→ 前端从 spool 分页看到结果;docker compose(api+worker+pg)跑通同样流程;
> 1.x 数据能 migrate 进 PG(允许个别字段失败)。

| # | 验收点 | 形态 | 证据 | 结论 |
|---|---|---|---|---|
| 1 | 解压/冷启动 → launcher 起 PG+API+worker | portable | CI `Cold-start (T6 hard evidence)` 绿(main 全绿 run);daily-server 实例本身即长期运行证据 | ✅ |
| 2 | 登录 | portable + compose | 两形态均真 token(curl,服务器端执行) | ✅ |
| 3 | MySQL 数据源,密码经 SecretStore | portable + compose | 创建 201;响应体零密码物料(R2);worker 后续能解密连接(下一条)反证加密入库正确 | ✅ |
| 4 | SQL 控制台 SELECT 经 worker → spool | portable + compose | job `success`;真 MySQL 8(25 行种子表) | ✅ |
| 5 | 前端从 spool 分页看到结果 | portable | API 层 `offset=0/20` 两页核对(10+5 行);浏览器实拍:成功徽章 + 25 行表格 + ColumnType 染色(数字右对齐/日期着色)+ 「1-25 / 共 25 行」分页控件 | ✅ |
| 6 | docker compose(api+worker+pg)同流程 | compose | **审计发现该形态此前不存在**(仓库仅 dev 单 PG 容器)→ 补齐于 PR [#35](https://github.com/allen-answer/dataOpsStudio_v2/pull/35);daily-server 隔离验证:build 一次过 → secrets-init→pg→migrate→api+worker 全 healthy → §5 流程全通(证据见 PR #35 评论) | ✅(待 #35 合并落档) |
| 7 | 1.x migrate 进 PG(允许个别字段失败) | — | CI `tests/integration/test_migrate_from_v1_pg.py`(真 PG 端到端:计数一致、密码解密-重加密往返、running→failed、字段级容错)随 main 常绿 | ✅ |

**总结论:contract §5 验收通过,2.0.0 骨架完成。**(#35 合并后即可打 tag。)

## 验收过程产出 / 遗留

- **补齐缺口**:docker compose 形态(PR #35,Dockerfile + compose.dataops.yml + 文档)。
- **建议追加 CI**:`Compose form (§5 hard evidence)` job(build + up + healthz),防该形态回归纸面交付。
- **dogfood 实例新增样本**:`acceptance-mysql` 数据源(指向同机 `dataops-v2-acceptance-mysql` 容器,25 行种子表)——首个可真实执行 SELECT 的数据源样本,保留。
- **体验 nit(不阻塞)**:SQL 工作台执行后不自动滚动到结果区;另见 PR #33 评论三条。
- **未决项(非 §5 范围)**:DM 真实例集成验证(Certified 宣称前必做,等持牌实例);Wave 4(Part B 后端补全)待立项。
- **基础设施观察**:本日 CI 三次红均为 GH Actions ↔ Docker Hub 拉镜像超时(重跑即绿);服务器对 GitHub clone 亦有一次超时。可考虑镜像代理。
