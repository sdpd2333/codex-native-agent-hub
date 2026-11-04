# 配置同步与选型规则

面板入口统一在「设置」下的「连接设置」与「选型规则」子页。运行预算、连接等待和目录页数等选项见 [应用设置](application-settings.md)。

连接设置先选择代理，再通过并列的「手动连接」和「本地客户端」标签配置。默认打开手动连接；标签切换保留草稿，不触发检测、导入或保存。检测结果和同步选项保留在各代理自己的本地客户端页。

## 本地连接来源

检测只由面板按钮触发。关闭/仅导入模式不会持续读取客户端文件；跟随模式按运行设置的维护间隔（默认 60 秒）、获取配置及新任务选型时读取已选来源。所有写入均在插件状态目录，不修改任何客户端文件。

| 代理 | 来源与优先级 | 导入范围 |
| --- | --- | --- |
| Claude | `CLAUDE_CONFIG_DIR` 或 `~/.claude/settings.json` | `env.ANTHROPIC_BASE_URL`、`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_MODEL` / `model`、`CLAUDE_CODE_EFFORT_LEVEL` / `effortLevel` |
| Gemini | `GEMINI_CLI_HOME`（默认 home）下的 `.gemini/settings.json`、`.gemini/.env` | `GEMINI_API_KEY` / `GOOGLE_API_KEY`、`GOOGLE_GEMINI_BASE_URL`、`GEMINI_MODEL` / `model.name`；仅 Gemini API Key 认证 |
| DeepSeek | `DSH_HOME` 或 `~/.dsh` 下 `config.yaml`、`cordis.patch.yml`、所选 `profiles/<name>/cordis.patch.yml`、`settings.yaml` | 读取 `agent-default-model`（兼容旧 `agent-loop` main）、`llm-deepseek`，或 `llm-pi-ai.providers` 的 `anthropic-messages` 连接；`.credentials.yaml` 的 `refs` 优先于配置内 Key / `.env` |

这是选定用户层来源的快照，不推断客户端当前会话、项目设置、系统管理设置、命令行参数或其他进程的环境变量。dsh profile 需明确选择，不猜测当前 profile；YAML 中的代码标记仅作为数据读取，绝不执行。

模型或必要连接字段缺失时，先在插件中保存完整连接，再只导入来源中存在的字段。OAuth、订阅登录、Key helper、动态表达式和不兼容协议会显示不可导入，不会尝试复制登录态或运行命令。

更改地址须同时导入该来源的 Key，避免把旧凭据发送给新地址。仅导入或跟随 Key 时，来源地址也必须与插件当前地址一致；客户端切换服务商会暂停跟随，需要重新同时选择地址与 Key。旧版保存的跟随关系也会重新验证地址。

`AGENT_HUB_<代理>_API_KEY` 环境变量仍优先于 DPAPI 密钥；与本次填写或导入的 Key 不一致时阻止手动保存和同步，需先移除或更新覆盖。地址与 Key 在同一配置锁内读取，网络请求使用该次快照；配置保存遇到同步写入错误时回滚凭据及设置。此回滚处理可捕获的写入异常，不保证突然断电或进程被强制终止时的多文件原子性。检测和规则接口从不返回 Key。

跟随时手动修改所选字段会暂停同步；跟随 Key 时，手动修改目标地址也会暂停。处理方法是在面板重新检测并应用，或关闭同步后独立管理。未选字段可以继续手动调整。客户端删除文件、移除凭据或改为不受支持的认证时，新任务停止使用该连接，已运行任务不受影响。

## 规则语义

规则由上至下排列，每条包含：

- 名称、开关和项目目录范围（目录及子目录，空白为不限）。
- 权限、复杂度、任务类型：字段之间 AND，同一字段的多个值 OR，空集合为不限。
- 自然语言意图：Codex 对实际任务判断是否匹配；空文本自动匹配。
- 候选池：从实时接口选择的完整模型 ID，绑定原生代理与其连接配置。可调整池内优先级。
- 选择偏好：给 Codex 的池内选择指导；不替代硬约束，也不能添加未授权候选。
- 推理强度：留空沿用连接设置；不支持该强度的代理被排除。这里只验证运行时支持值，不保证具体模型一定支持该强度。
- 回退开关：该规则池无可用项时，是否继续后续匹配规则及默认池。池内多个候选本身按顺序作为备选。

第一条命中且有可用候选的规则决定池。列表获取失败、连接未完成、同步冲突、SDK 未安装、模型消失或强度不支持的项会被排除。规则没有可用候选且禁止回退时立即停止；默认池也不可用时停止。规则模式不会悄悄采用池外默认模型。

候选名称必须能识别为相应家族的通用模型；未知别名和图像、嵌入等专用模型不进入自动池。名称识别不代表实际能力、价格、延迟或工具兼容性已验证。明确手动选择仍可使用服务商别名。当前每个 backend 一套独立连接，不提供同一 backend 的多账户管理或网络请求代理。

例如可先创建“复杂编辑”规则（编辑 + complex），候选池选自己可用的 Claude / DeepSeek 型号，偏好写“优先擅长架构与测试的候选”；后面创建“日常只读”规则，选择 Gemini 等可用型号。这里不写死型号，须从自己的实时列表选择。价格偏好需要有可信价格依据，不按名字虚构费用。

## MCP 与技能

`get_configuration` 返回规则开关/配置及每个连接的同步状态。`nah-delegate` 技能只说明以下流程，规则内容保存在插件的 `routing.json`，不复制到技能文本。

1. `get_routing_context(task, project_dir, mode, task_complexity, task_kind, requested_backend)` 返回实时候选、满足硬条件的规则及有效 10 分钟的 token。仅当用户指定代理时设置 `requested_backend`。
2. Codex 判断所有非空 intent，把匹配规则 ID 放入 `matched_intents`。`preview_routing(token, matched_intents)` 返回首个可用池、偏好、排除与回退记录。面板由用户手动标记意图，不假装调用了 Codex 分类。
3. Codex 在该池选择 `candidate_id` 并给出 `reason`。向 `submit_task` 传相同任务与条件，以及 `routing_decision={token, matched_intents, candidate_id, reason}`，省略 `model`。
4. 服务检查任务绑定、规则版本、连接版本与凭据版本；启动前再检查实时可用项。候选消失时按已匹配规则回退。决策 token 单次使用，过期/重启/任务或配置变化后重新获取。
5. 记录实际 backend/model、规则版本、连接版本、理由、排除和回退。原生 SDK 启动后不自动切换，避免重复文件/工具副作用。

用户为本任务明确指定型号时，继续用 `model` + 引用实际用户指令的 `model_override_reason`，优先于规则。Codex 按规则选型不属于用户指定，不能伪造 override reason。规则关闭时保留原有固定型号与代理内自动选型。

本机制选择被委派的原生代理和模型，不切换宿主 Codex 模型。不同代理仍使用自己的 SDK；客户端工具/提示词/权限不随连接导入。

## 设计参考

参考 Magpie 的有序规则、候选池、强度约束、可解释回退思路，在任务级实现原生运行时委派。没有复制其网关或引入 Magpie 运行依赖。

- [Magpie 路由规则结构](https://github.com/yetone/magpie/blob/37cc86b3b9d30b45b64029c89fd73b4b04059a5b/internal/provider/grouprule.go)
- [Magpie 规则执行](https://github.com/yetone/magpie/blob/37cc86b3b9d30b45b64029c89fd73b4b04059a5b/internal/gateway/rules.go)
- [Claude Code 设置](https://code.claude.com/docs/en/settings)
- [Gemini CLI 配置](https://geminicli.com/docs/reference/configuration/)
