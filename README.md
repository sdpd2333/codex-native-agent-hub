# Codex Native Agent Hub

A unified Codex MCP bridge and local panel for Gemini / Antigravity, Claude Code, and DeepSeek Harness, with isolated worktrees and live task progress.

在 Codex 右侧完成模型接入、任务委派、进度查看与变更检查。三种模型分别使用自己的原生编码运行环境，连接配置和 API Key 独立保存。

**Windows x64 · Python 3.11+ · Git · MCP stdio · MIT**

```text
Codex / 本地面板
       │
       ├─ Gemini   → Antigravity SDK
       ├─ Claude   → Claude Code / Agent SDK
       └─ DeepSeek → DeepSeek Harness / dsh SDK
                         │
                   对应原生协议的模型服务
```

本项目通过 MCP 接入 Codex，无需修改客户端。右侧面板用于配置和查看任务，代理返回结果后由 Codex 检查。

## 能做什么

- 分别配置 Gemini、Claude 和 DeepSeek，从服务商拉取模型列表并测试连接。
- 手动选择模型，或开启按任务复杂度自动选型。
- 可选检测并导入本地客户端连接，或单向跟随所选配置。
- 用有序自定义规则跨代理选择型号，保留各自的原生 SDK、任务权限和工作区。
- 提交只读审查或编辑任务，编辑时默认使用独立 Git worktree。
- 查看任务进展、结果与代码变更，随时取消任务。
- 按对话管理历史、归档已完成任务，并清理旧运行日志。

## 安装

准备 Windows x64、Python 3.11+、Git 和可在终端使用的 Codex CLI。

```powershell
git clone https://github.com/sdpd2333/codex-native-agent-hub.git
cd codex-native-agent-hub
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

脚本创建项目内 `.venv`，安装三套 SDK 及所需运行组件，并注册名为 `native-agent-hub` 的 MCP 服务。**无需另外安装 `agy`、Gemini CLI、Claude Code CLI 或 `dsh` 命令行。**

可选：安装面板入口技能，之后可用 `$nah` 或「打开 NAH 面板」打开当前对话和项目的面板：

```powershell
.\scripts\install-nah-skill.ps1
.\scripts\install-nah-skill.ps1 -SkillName nah-delegate
```

第二条命令安装 `$nah-delegate`，用于按实时规则委派任务。也可在首次安装时加上 `-InstallSkill` 安装两个技能。技能默认安装到 `~/.codex/skills/`（设置了 `CODEX_HOME` 时使用该目录下的 `skills/`）；若当前对话尚未识别，请重新加载技能或新开对话。安装器会保留并提示用户修改过的技能文件。

## 从右侧面板开始

重新加载 Codex 的 MCP 服务，或重新打开 Codex，然后说：

> 打开 native-agent-hub 的统一面板，放在右侧，使用当前项目和当前对话。

1. 在「设置 → 连接设置」选择 Gemini、Claude 或 DeepSeek。
2. 填写该代理的 **Base URL 和 API Key**，点击「拉取模型」，选择完整模型 ID 后保存。
3. 点击「测试基础生成」确认连接。测试使用少量接口额度，**不会保存配置**；修改设置后需另行保存。
4. 在「新建任务」选择代理、项目目录和任务权限，填写目标。建议先提交「读取 README 并概括项目用途」这样的只读任务，验证工具调用。
5. 在「任务进展」查看活动与结果；编辑任务可点击「查看变更」，检查工作区路径和代码差异。

执行结果默认按 Markdown 预览，支持标题、列表、表格和代码块；点击「查看原文」可切换显示。「复制完整结果」会读取所有分页并复制原始文本，保留 Markdown 标记。图片显示为文字说明，原始 HTML 作为文本展示。

三套配置可使用同一中转站，也可使用不同服务，API Key 需分别填写；更换地址时需重新填写 Key。保存设置只影响之后提交的任务。

「设置」统一包含连接设置、选型规则、运行设置、界面设置、存储与归档五个子页。在「运行设置」可以保存并发数（默认 2，范围 1–8）、任务默认超时、模型与工具调用预算、Claude 轮次、DeepSeek 输出额度、连接与 Git 超时、模型目录页数及维护间隔。在「界面设置」调整刷新频率、任务/结果分页、默认 Markdown 或原文、活动条数与差异预览大小。各字段标明默认值和适用范围，恢复默认值需要另行保存。

应用设置跨对话、项目及使用同一状态目录的新版服务共享，保存后后续操作读取新值。每个服务实例分别计算并发名额；降低上限不会终止已有任务，满额时仍直接拒绝新任务。运行预算与任务超时在提交时保存快照，已有任务及其预算显示不受后续设置影响。任务默认 600 秒，可设置为 10–86400 秒，单次提交仍可覆盖。`AGENT_HUB_MAX_CONCURRENT_TASKS` 仅覆盖设置了该变量的进程；面板明确显示覆盖来源与本地保存值，并锁定该输入，移除覆盖并重启后可编辑。完整字段与生效规则见 [应用设置](docs/application-settings.md)。

双击 `configure.cmd` 会在普通浏览器打开面板。也可手动启动：

```powershell
.\.venv\Scripts\python.exe -B server.py --panel --project-dir C:\Projects\my-app
```

升级后请重启 MCP 服务和独立面板，再从当前对话重新打开；服务重启后旧面板地址会失效。

验证当前 checkout 的修改时，可使用 `--panel --new-instance` 启动新面板进程，避免复用已加载旧代码的服务。新旧实例共享已保存的连接及历史，分别计算并发名额；不会停止已有服务或任务。

## 自动选择适配模型

在「设置 → 连接设置」为需要的代理开启「自动选择适配模型」（默认关闭）。开启后，根据任务复杂度从实时模型列表中选择型号：对话中由 Codex 判断复杂度，面板中由你选择「简单」「常规」或「复杂」。

你为本次任务明确指定的型号优先，临时选型不会修改默认配置。任务详情会显示实际模型与选择原因。选型按模型名称估算能力；无法匹配或拉取列表失败时，沿用已保存的默认模型。

## 本地配置检测与同步

「设置 → 连接设置」提供并列的「手动连接」与「本地客户端」标签，默认显示手动连接，可直接填写地址、API Key 和模型；切换标签会保留未保存的草稿。

在「本地客户端」中可选择关闭、导入一次或跟随配置。默认关闭；点击检测、检查来源与字段后才会导入。跟随模式按运行设置的维护间隔（默认 60 秒）、读取配置及新任务选型前检查。只读取连接字段，不写回客户端，不导入工具、MCP、Skills、提示词或权限。支持本地 Claude Code、Gemini CLI 与 dsh 配置，以及 CC Switch 对这些客户端文件的更新。

手动修改正在跟随的字段会暂停同步并提示冲突；未选择跟随的字段仍独立管理。来源缺失或不兼容时停止用该连接启动新任务，已有任务保留配置快照。OAuth/订阅凭据和动态 Key helper 不作为 API Key 导入。完整范围见 [配置同步与选型规则](docs/configuration-routing.md)。

## 跨代理自定义规则

在「设置 → 选型规则」刷新实时候选，添加并排序规则，设置项目范围、任务权限、复杂度、任务类型、自然语言意图、候选池和选择偏好。可设推理强度及是否允许回退，再配置默认候选池并启用保存。各硬条件同时满足；第一条匹配且有可用候选的规则决定本次候选池。

对话委派时，Codex 读取最新规则与候选，判断意图并在允许的池内选择，插件校验 ID、连接版本与规则版本。每个候选绑定该原生代理的独立连接和完整模型 ID；当前每个代理保存一套连接，可包含多个模型。规则模式支持 DeepSeek Pro 等可识别通用型号，不受旧版代理内 Flash 策略限制。用户明确指定型号时优先。

在「新建任务」选择跨代理规则，填写任务与项目后读取规则、手动勾选匹配意图并点击「试选并解释」。试选不启动任务，也不调用额外的分类模型。任务记录保存规则名称、版本、实际组合、理由和回退过程。自动回退只发生在任务启动前；原生工具开始运行后失败会保留结果和工作区，不自动换代理重做。

该设计参考 Magpie 的有序规则、候选池和可解释回退，在本项目中作为任务委派规则实现；不引入网关或协议转换依赖。详见 [规则配置与 MCP 调用流程](docs/configuration-routing.md)。

## 中转站的协议要求

| 代理 | 服务商需要支持的接口 |
| --- | --- |
| Gemini | `/v1beta/models/*:generateContent`、`streamGenerateContent` 和函数调用 |
| Claude | Anthropic `/v1/messages`，支持 SSE、工具调用及 Claude Code 请求格式 |
| DeepSeek | **DeepSeek Messages `/v1/messages`**，支持 SSE 与工具调用 |

**当前 DeepSeek 运行环境使用 Messages 协议，只提供 OpenAI `/chat/completions` 的服务不够。** 请按服务商提供的兼容端点填写地址。

Base URL 使用 HTTPS；`localhost`、`127.0.0.1`、`[::1]` 等本机回环地址可使用 HTTP，便于连接 CC Switch 或本地网关。可保留服务商要求的路径前缀，末尾的 `/v1` 或 `/v1beta` 会自动处理。

模型列表可见或基础生成成功，还需通过小型任务确认流式响应与工具调用可用。当前 DeepSeek 只读模式不提供目录搜索工具，请在任务中指定需要读取的文件。

## 编辑任务与 worktree

默认的「只读审查」只能读取项目、分析并提出建议，无需 Git。「编辑执行」允许修改文件和执行命令，默认在独立 Git worktree 中进行。

- 使用独立 worktree 时，源仓库必须**没有未提交更改，且至少已有一次提交**。它从当前提交创建，未跟踪、忽略的文件和已安装依赖不会自动复制。
- 项目不满足条件时，可先整理并提交改动，或明确选择「直接编辑项目目录」。
- 任务结束、失败或取消后，工作区和已有修改都会保留，**不会自动提交、合并或清理**。
- 完成后点击「查看变更」，检查差异与新增文件，并核实测试结果，再将需要的修改合并回源项目。

worktree 用于隔离代码变更，不是操作系统沙箱；仅对你授权的任务开放编辑模式。

## 在对话中委派

> 用 native-agent-hub 的 Claude 只读审查当前项目，完成后核实发现的问题。

> 用 native-agent-hub 的 DeepSeek 实现这个模块，允许修改代码和运行测试，使用独立 worktree。完成后检查变更和测试结果。

## 任务记录与本地数据

- **查找历史**：任务列表默认显示「本对话」，切换到「全部任务」可查看其他对话及未归属的旧任务，也可搜索或按状态筛选。
- **关注结果**：默认按运行中、最近完成、需要关注、更早完成分组。最近完成按结束时间优先展示，默认 3 个，可在「设置 → 界面设置」调整；最近完成、异常和更早完成可独立折叠，默认展开，刷新保留选择和折叠状态。
- **归档与恢复**：默认在任务成功完成满 7 天后自动归档，可在「设置 → 存储与归档 → 归档规则与旧任务整理」中调整或关闭，规则适用于本机所有对话。失败或取消的任务可手动归档；在「已归档」中恢复后，不再自动归档。
- **释放空间**：归档保留结果和工作区，不释放磁盘空间。进入「设置 → 存储与归档」，选择日志保留天数（默认 30 天），预览后手动清理；任务记录、最终结果、连接配置和编辑工作区都会保留。

配置、任务记录和工作区默认保存在 `%LOCALAPPDATA%\CodexNativeAgentHub`，API Key 使用 Windows 当前用户的 DPAPI 加密。

模型服务会接收任务及代理读取的内容。日志和结果可能包含文件内容与个人路径，请勿上传状态目录；面板地址带有访问令牌，请勿公开分享。

## 参考与许可

- [开发与验证](docs/development.md) · [更新日志](CHANGELOG.md) · [安全说明](SECURITY.md)
- [Antigravity SDK](https://pypi.org/project/google-antigravity/)
- [Claude Agent SDK](https://platform.claude.com/docs/en/agent-sdk/python)
- [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
- [Codex MCP](https://developers.openai.com/codex/mcp/)

本项目从 [codex-gemini-antigravity](https://github.com/sdpd2333/codex-gemini-antigravity) 的本地面板与 MCP 桥接方案扩展而来，以独立仓库维护。项目代码采用 [MIT License](LICENSE)；第三方 SDK 及运行组件按各自条款分发。Markdown 解析器及其内含依赖的许可见 [第三方许可](vendor/markdown-it-LICENSE.txt)。
