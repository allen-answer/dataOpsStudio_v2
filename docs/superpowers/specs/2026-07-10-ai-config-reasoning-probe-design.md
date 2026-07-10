# AI 配置连接测试兼容推理模型

## 背景

管理后台的 `POST /api/admin/ai-config/test` 使用 `max_tokens=8` 发送最小探测请求。
部分 OpenAI-compatible 推理模型会先生成 `reasoning_content`；在 8 token 上限内，
请求虽然成功，但正式 `content` 仍为空且 `finish_reason=length`。现有解析器正确拒绝
空 `content`，页面因此把可用连接误报为 `ProviderError`。

## 方案

只把管理后台连接测试的输出上限从 8 提高到 256。正常 AI 调用、provider 解析器、
数据出站策略和密钥处理保持不变。256 已在同一已配置 provider 上验证可产生非空
`content`，同时仍是小规模连通性探测。

不使用 `reasoning_content` 充当正式回答，因为推理内容不等价于 provider 的最终输出；
也不增加自动重试，避免一次“测试连接”产生多次外部调用和额外费用。

## 测试

在管理后台路由单元测试中注入捕获型 gateway，断言连接测试传入
`AiOptions.max_tokens == 256`，并保持成功响应和审计断言。测试先在现有实现上失败，
再修改生产代码使其通过。随后运行相关 AI/admin 单元测试、ruff、mypy、项目红线检查
及敏感信息扫描。

## 发布

改动提交到 `agent/fix-ai-config-reasoning-probe` 分支，推送 GitHub 并创建 Draft PR；
不直接合并 `main`。
