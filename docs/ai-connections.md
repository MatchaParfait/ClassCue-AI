# AI 服务连接

设置 → AI 服务顶部的“已保存连接”可直接切换用于新任务的连接。

新连接与未填写输出预算的配置默认使用 16,384 token，前端、HTTP 接口和命令行适配器保持一致。已有连接显式保存的预算继续保留，可在高级参数中调整。调用缓存包含输出预算和思考强度，避免沿用不同参数的旧结果。

新连接与未填写相应参数的配置默认请求超时为 120 秒、最大重试次数为 5（初次请求之外最多重试 5 次）。已有连接显式保存的值继续保留。

## 思考强度说明（2026-10-16 核对）

思考强度位于模型名称下方，只提供 Low / Medium / High 三档，默认 High。旧连接或旧客户端传入的缺失、空值及 `null` 都按 High 处理；已保存的 Low / Medium 保留。设置页标注“GPT-6 Luna、DeepSeek 4.1 Flash 建议最低使用 Medium；默认 High”，这是本项目的使用建议。

正式提取、连接测试及 JSON 修复均显式发送所选 `reasoning_effort`。连接测试与修复沿用连接配置的输出预算，为思考过程预留空间。历史“服务端默认”选项已移除；以下保留不传参数时的上游行为说明，便于理解不同服务的差异。

- GPT：Chat Completions 使用 `reasoning_effort`，Responses 使用 `reasoning.effort`。各模型支持的档位和默认值存在差异，应查具体型号。当前官方 reasoning guide 对 GPT-6 Luna/Sol 标注默认 `medium`，不能据此断定同名中转模型也采用相同设置。
- DeepSeek：当前官方思考模式文档规定默认开启思考，默认强度 `high`。原生强度为 `low / high / max`；兼容输入 `medium` 映射为 `high`，所以它不提供与 GPT 对等的 Medium 档。Chat Completions 同样使用 `reasoning_effort`，并提供 `thinking.type` 开关；当前文档也支持以 `reasoning_effort=none` 关闭思考。这些规则不能直接套用到旧模型或第三方部署。
- sub2api：核对的是公开 `main` 源码，不是用户部署版本。Chat Completions 转 Responses 时，只有调用方显式提供强度才写入 `reasoning.effort`；Anthropic 转 Responses 的另一条路径会在缺省时补 `medium`。因此它没有跨所有入口和模型都一致的默认档位，实际值还可能受模型映射、后台策略及上游默认值影响。

核对来源：

- [DeepSeek 官方 Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)
- [DeepSeek 官方 Chat Completions 参数](https://api-docs.deepseek.com/api/create-chat-completion)
- [OpenAI 官方 Reasoning models](https://developers.openai.com/api/docs/guides/reasoning)
- [OpenAI 官方 Chat Completions 参数](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
- [sub2api Chat Completions 转 Responses 实现](https://github.com/Wei-Shaw/sub2api/blob/main/backend/internal/pkg/apicompat/chatcompletions_to_responses.go)
- [sub2api Anthropic 转 Responses 实现](https://github.com/Wei-Shaw/sub2api/blob/main/backend/internal/pkg/apicompat/anthropic_to_responses.go)

- 新增：在本地编辑空白草稿，填写名称、地址、Key，获取或填写模型；保存后才建立并启用连接。
- 复制：以当前已保存配置建立草稿；保存时复制 Key，新副本拥有独立的密钥记忆。修改副本地址后不会携带原地址的 Key。
- 重命名：直接编辑连接名称，与其余字段一起保存。
- 删除：确认后移除连接；删除当前连接时选择列表首项。允许删除最后一个连接，随后显示未配置状态，刷新后保持为空，可点击“新增”重新配置；运行中的任务不受影响。
- 切换、新增、复制、删除前，如果配置或 Key 尚未保存，可选择保存并继续、放弃修改并继续或取消。
- 同一 Base URL 可以保存不同模型、不同账号的连接。新的连接不会继承同址其他连接或环境变量中的 Key。
- “保存连接”一次保存名称、地址、Key、模型和高级参数；配置写入失败会恢复此前的加密密钥。
- 地址旁的 DeepSeek / OpenAI 按钮仅用于快捷填写地址，不切换账号或自动更换模型。自定义兼容服务直接粘贴地址。
- 测试连接与获取模型直接使用当前表单的地址、Key 和参数，不修改已保存配置。获取模型无需先填写模型名称。
- Key 留空保留已保存值；点击移除只暂存删除操作，保存后生效，可在保存前恢复。

原有单套配置自动作为 `default` / “当前连接”读取，已有按地址保存的密钥继续可用。首次修改时写入多连接结构；普通 JSON 配置只含连接 ID、名称、模型和参数，密钥仍由 Windows DPAPI 加密保存。查看明文必须通过原有显式操作，切换连接后隐藏明文。

`ai_providers.json` 保留 `profile` 作为当前连接兼容视图，并增加 `connections`、`active_connection_id`。写入使用临时文件替换；HTTP 修改在文件锁内执行。旧版保存接口只更新当前连接，不覆盖其他连接；带 `connection_id` 的过期保存会返回 409。

接口：

| 操作 | 请求 |
| --- | --- |
| 读取当前配置和连接摘要 | `GET /api/settings/ai` |
| 更新当前配置 | `PUT /api/settings/ai` |
| 新增／复制并启用 | `POST /api/settings/ai/connections`，`name`、可选 `source_id` |
| 切换 | `POST /api/settings/ai/connections/{id}/activate` |
| 重命名 | `PATCH /api/settings/ai/connections/{id}`，`name` |
| 删除 | `DELETE /api/settings/ai/connections/{id}` |

更新配置接口支持 `connection_name`、可选 `api_key` 或 `remove_key`；创建连接支持完整 `settings` 和可选 Key。测试与获取模型支持 `settings`、`api_key` 草稿。单独的旧 Key / 重命名接口继续兼容已有调用方。密钥按连接标识和规范化地址隔离；改地址后不会把原地址的 Key 发送到新地址。删除 Key 后会记录禁用标记，阻止旧密钥或环境变量意外回退。

单视频任务和批量任务在提交时取得连接参数和 Key 的内存快照；切换或编辑连接不会改变正在运行的任务。显式重试是新一次执行，可采用最新配置，保留原有失败恢复流程。

验证：

```text
.venv/Scripts/python.exe -m pytest tests/test_ai_connections.py tests/test_ai_provider.py tests/test_api.py tests/test_web_batch.py tests/test_ai_cli.py tests/test_ai_ui.py -q
cd frontend
npm test -- src/pages/SettingsPage.connections.test.tsx src/pages/SettingsPage.test.tsx src/components/SavedApiKey.test.tsx src/api/client.test.ts
npm run build
```

`node tests/frontend_ai_connections.cjs` 使用独立临时配置、数据库及本机加密凭据，验证迁移、增删改、复制密钥隔离、切换前草稿、重载持久化和 1338 / 856 / 390px 布局。AI 测试与模型列表由本地替身返回，不向外部服务发送请求。
