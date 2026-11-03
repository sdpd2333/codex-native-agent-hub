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

这是通过 MCP 接入的外部代理服务。右侧界面是本项目的本地网页，模型返回结果后由 Codex 检查。它不会把第三方模型注册为 Codex 内部的 `spawn_agent` 模型，也不需要修改 Codex 客户端。

## 能做什么

- 在一个面板中为三种代理分别设置 Base URL、Key、模型和适用的推理强度。
- 从中转站拉取模型列表，保留准确 ID，搜索、选择或为本次任务覆盖模型。
- 可为每个代理开启「自动选择适配模型」（默认关闭），由 Codex 判断复杂度后按模型名称为单次任务选型。
- 按原生协议测试基础生成；通过任务验证流式响应与真实文件工具。
- 默认只读审查；编辑任务默认创建独立 Git worktree，保留变更供检查和合并。
- 查看实际阶段、工具活动、调用次数、耗时、结果和历史；支持取消与超时。
- 按对话筛选任务、手动归档与恢复；查看存储占用，预览后清理旧运行日志。
- 每个服务实例最多同时运行两个任务，每个任务限制 40 次模型调用、100 次工具调用。
- Windows DPAPI 按当前用户加密凭据；状态、会话和 worktree 默认存储在仓库外。

原生运行环境提供各自的系统提示、工具与执行流程；实际效果仍取决于模型、任务和中转站兼容性。

## 安装

准备 Windows x64、Python 3.11+、Git 和可在终端使用的 Codex CLI。

```powershell
git clone https://github.com/sdpd2333/codex-native-agent-hub.git
cd codex-native-agent-hub
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

脚本创建项目内 `.venv`，安装固定版本的三套 SDK，并注册名为 `native-agent-hub` 的 MCP 服务。路径按实际安装位置生成。已有的 `gemini-antigravity` 服务不会被覆盖。

固定版本的 Windows SDK 包含各自所需的原生运行组件，**无需另外安装 `agy`、Gemini CLI、Claude Code CLI 或 `dsh` 命令行**。首次安装仍需下载 Python 依赖和运行组件；它们不会提交到本仓库。

仅安装依赖，不注册 MCP：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -SkipMcpRegistration
```

## 从右侧面板开始

重新加载 Codex 的 MCP 服务，或重新打开 Codex，然后说：

> 打开 native-agent-hub 的统一面板，放在右侧，项目目录使用当前项目。

Codex 调用 `get_panel(project_dir=项目绝对路径, conversation_id=当前对话ID)`，再通过桌面应用的 `open_in_codex` 打开返回的 URL，设置 `target.type="browser"`、`placement="right"`。普通 MCP 客户端也可在浏览器打开同一面板。

1. 在「连接设置」选择 Gemini、Claude 或 DeepSeek。
2. 填写该代理使用的 **Base URL 和 API Key**，点击「拉取模型」，选择完整模型 ID 后保存。
3. 「测试基础生成」会发送一条简短请求，使用少量服务额度；测试不会保存配置，修改后需要另行保存。
4. 在「新建任务」选择代理、项目、模式和目标，先用小型只读任务验证工具兼容性。
5. 在「任务进展」查看活动和结果；编辑任务可「查看变更」，读取 worktree 路径、分支与差异。

三套配置可以指向同一中转站，也可指向不同服务；Key 不会在代理之间自动复用。更换地址时必须重新填写 Key。任务提交时固定当时的配置，之后保存设置只影响新任务。

切换代理会在当前页面保留未保存草稿（包括内存中的 Key）；关闭或刷新页面会清除草稿。如果其他面板或 MCP 已更新同一代理，保存会提示版本冲突，避免覆盖新配置。可记录需要的修改，点击「重新加载已保存配置」后合并。

任务列表默认显示「本对话」，也可切换「全部任务」和「已归档」。运行中的任务优先显示，已完成任务可折叠；另有其他对话运行中任务的入口。搜索和状态筛选在分页前执行，每页最多 50 条摘要，可继续翻页查找旧任务。选中任务后按需加载描述和结果，可「加载更多」或「复制完整结果」。页面隐藏时暂停自动请求，暂停状态下仍可手动刷新。运行中读取的差异会标明可能过期，任务结束后自动更新一次；后续手动修改工作区仍需点击「刷新变更」。

任务结束后可手动归档，归档任务可恢复；运行中任务不能归档。归档仅改变列表展示，不删除数据、不停止任务，也不释放磁盘空间。旧任务缺少对话 ID 时显示「未归属」，可在「全部任务」查找，不会猜测或批量归入当前对话。

`get_panel` 和同一对话中的每次 `submit_task` 应传相同的 `conversation_id`。优先使用宿主提供的稳定对话 ID；宿主没有 ID 时，调用一次不带 ID 的 `get_panel`，在本对话后续调用中复用返回的 ID。每次不带 ID 打开面板都会生成新的归属标识，不能根据项目路径或上一个面板推断对话。未传 ID 的旧客户端任务保留为未归属。面板地址绑定固定的对话与默认项目，多个面板不会互相覆盖。

任务标题与完整描述独立保存。可填写最多 80 字符的简短标题；省略时使用完整的短句或项目标签，不再把长提示词直接当标题。详情中的「任务描述」可展开查看全文，运行中的任务也可读取；描述仅在展开时加载，不进入列表轮询。历史任务若只保存了标题片段，会明确标注描述不完整，无法恢复旧版未保存的文字。

升级后请重启 MCP 服务和独立面板，并重新打开面板地址，使网页与接口版本一致。

双击 `configure.cmd` 会在普通浏览器打开面板。也可手动启动：

```powershell
.\.venv\Scripts\python.exe -B server.py --panel --project-dir C:\Projects\my-app
```

加 `--no-browser` 只输出本机面板地址。服务结束后旧地址失效，重新获取即可。

## 自动选择适配模型

在「连接设置」开启「自动选择适配模型」（默认关闭）。Codex 判断任务复杂度，服务根据当前代理的**实时模型列表**选型。

- **Gemini**：简单任务先限定可用的基础档（优先 Flash），再选择该档低位；常规与复杂任务按完整的版本号、能力梯度排序。同版本 Flash < Pro / Ultra / Argon，同版本同能力合为一组。
- **Claude**：Haiku 为快速、Sonnet 为均衡、Opus / Fable 为高能力。按下表选档，同档优先较新版本，同版本优先稳定版。
- **DeepSeek**：在 Flash 型号中选择，同档优先稳定版和较新版本。

| 任务复杂度 | 典型任务 | Gemini 实时梯度位置 | Claude 档位顺序 |
| --- | --- | --- | --- |
| `simple` | 连通性检测、仅回复 OK、文案、局部小修 | 最低可用能力档内的低位 | 轻量 → 快速 → 均衡 |
| `standard` | 常规功能、测试、排错 | 中位（向下取整） | 均衡 → 快速 → 高能力 |
| `complex` | 架构、困难排错、大范围重构 | 高位 | 高能力 |

Gemini 组内优先稳定版、旗舰系列及已保存模型。

候选限于当前代理可识别的通用编码型号，保留原始 ID。无法匹配或列表失败时沿用默认模型。规则按名称估算能力。

手动指定模型优先，自动选型仅影响本次任务。对话中由 Codex 判断复杂度，面板新建任务时由用户选择；任务详情显示实际模型与选型原因。只有用户为**本次任务**明确点名型号才算显式覆盖；提及代理、「这三个模型」、上一任务使用的型号或已保存默认值均不能作为覆盖依据。

MCP 调用：`get_configuration` 返回 `model_selection_policy`，`list_models` 返回名称分类。自动选型时向 `submit_task` 传入 `task_complexity` 并省略 `model`；用户明确指定时传入完整 `model`，并用 `model_override_reason` 引用用户指定型号的原话依据（最多 300 字符）。仅传 `model` 会被拒绝，避免复制默认配置意外绕过选型；面板手动选型会自动记录其来源。

纯原生 SDK 连通性检测使用 `task_kind=connectivity`、`mode=review`，省略 `model`。服务将复杂度固定为 `simple`，优先基础模型（Gemini Flash、Claude Haiku、DeepSeek Flash）；仍保留用户明确指定型号的优先权。自动选型关闭时继续使用默认模型；模型目录获取失败或缺少可识别候选时，会显示回退原因。排查连通性故障、审查代码等实际工作应使用 `general`，不会因为提示词包含「OK」就被降档。

`test_connection` 和连接设置中的「测试连接」是直接 HTTP 协议探测，使用所选默认型号；要求官方客户端 UA 或验证原生 SDK 链路时，应通过上述 `submit_task` 连通性任务。重新加载 MCP 服务后，新参数及调用约束才会提供给客户端。

### 自定义策略

策略文件：项目根目录的 [`model_selection.py`](model_selection.py)。

| 位置 | 可以调整什么 |
| --- | --- |
| `NAME_RULES` | 名称关键词与能力分类 |
| `VERSION_FIRST` / `TIER_ORDER` | 使用版本梯度的代理、同版本的能力顺序 |
| `LATEST_IN_TIER` | 同档优先版本号的代理，默认 Claude |
| `COMPLEXITY_POSITIONS` | 各任务复杂度在实时梯度中的相对位置，取值 `0～1` |
| `PREFERENCES` | 按档位选型的优先顺序，默认用于 Claude / DeepSeek |
| `COMPLEXITIES` | 任务复杂度的判断说明 |
| `FLAGSHIP_HINTS` / `SPECIALIZED` | 旗舰系列提示词、专用模型过滤词 |
| `_version` / `choose_model` | 版本解析与选择逻辑 |

修改后重新加载 MCP 服务并重启面板。模型列表在每次任务选型时重新拉取、评估。

## 中转站的协议要求

| 代理 | 原生运行环境 | 模型调用接口 | 只读工具 |
| --- | --- | --- | --- |
| Gemini | `google-antigravity==0.1.23` | `/v1beta/models/*:generateContent`、`streamGenerateContent` 和函数调用 | 浏览、搜索、查找、读取 |
| Claude | `claude-agent-sdk==0.2.176` | Anthropic `/v1/messages`，支持 SSE、工具调用及 Claude Code 请求格式 | Read、Glob、Grep |
| DeepSeek | `deepseek-harness-sdk==0.2.1rc2` | **DeepSeek Messages `/v1/messages`**，支持 SSE 与工具调用 | read、working_directory |

**此处固定的 dsh 版本使用 Messages 协议。只提供 OpenAI `/chat/completions` 的路径不够。** 根据你的服务商提供的兼容端点填写地址；不能仅凭模型名称判断协议。

Base URL 使用 HTTPS，可保留服务商要求的路径前缀。末尾 `/v1` 或 `/v1beta` 会被归一化，再添加对应接口路径。模型列表优先使用 `/v1/models`，Claude 支持分页；Gemini 必要时回退 `/v1beta/models`。

列表中可见、基础生成成功，均不能保证完整代理流程可用。SDK 会发送工具定义和流式请求，需要中转站完整转发。当前 DeepSeek 只读模式使用精简原生配置，尚不提供目录搜索工具；可在任务中指定需要读取的文件。编辑模式使用完整编码工具配置。

## 编辑任务与 worktree

`edit` 默认以源仓库当前 HEAD 创建独立分支 `agent-hub/<代理>/<任务ID>` 与 worktree，因此源仓库必须干净、已有提交。两个任务使用不同目录，避免直接覆盖彼此的修改。worktree 从已提交内容开始，未跟踪、忽略的文件和已安装依赖不会自动复制；必要时可在任务中要求安装依赖。

源项目有未提交更改或不是 Git 仓库时，可以先处理仓库状态，或在面板中明确选择「直接编辑项目目录」。只读任务始终读取指定目录，无需 Git。

任务结束、失败、取消或超时后，worktree 及已发生的修改都会保留。面板与 `get_changes` 返回已跟踪文件相对起点的差异和新增文件名；新增文件内容需要在工作区进一步查看。不会自动提交、合并或清理。

检查结果与测试后，可手动将需要的修改提交并 cherry-pick 到源分支：

```powershell
git -C <worktree路径> diff
git -C <worktree路径> status --short
# 选择文件后提交，再在源项目中 cherry-pick 该提交。
```

`review` 通过原生工具白名单禁用写入与命令执行。`edit` 允许文件修改和命令执行；worktree 用于隔离版本变更，**不是操作系统沙箱**。只对你授权的任务开放编辑模式。

## 在对话中委派

> 用 native-agent-hub 的 Claude 审查当前项目。先读取配置，再以 review 模式提交，最后核实发现的问题。

> 将这个模块的实现交给 DeepSeek，通过 dsh 执行。允许修改代码和运行测试，使用独立 worktree。完成后检查 get_changes 和测试结果。

| MCP 工具 | 用途 |
| --- | --- |
| `get_configuration(backend)` | 三套配置的就绪状态、所选模型和已安装 SDK 版本 |
| `get_panel(project_dir, conversation_id)` | 获取当前对话面板地址，可预填项目路径；返回对话 ID |
| `list_models(backend, query)` | 拉取完整模型 ID，空查询返回所有模型 |
| `select_model(model, backend)` | 按用户要求保存默认模型 |
| `test_connection(backend)` | 用保存的配置测试原生文本生成，会使用接口额度 |
| `submit_task(...)` | 提交任务，立即返回任务 ID 和面板地址 |
| `get_job(job_id, ...)` | 查询状态、活动与结果，支持等待和结果分页 |
| `get_changes(job_id)` | 读取工作区差异和新增文件名 |
| `cancel_job(job_id)` | 终止本机工作进程树，保留文件修改 |

`submit_task` 参数包括完整描述 `task`、可选短标题 `title`、`conversation_id`、绝对路径 `project_dir`、`backend=gemini|claude|deepseek`、`mode=review|edit`、`task_kind=general|connectivity`、可选 `model`（须配合 `model_override_reason`）、可选 `task_complexity=simple|standard|complex`、`workspace_mode=worktree|direct`、`timeout_seconds`。默认 Gemini、review、general、worktree，超时 600 秒（可设 10–1800 秒）。`get_job` 默认返回完整脱敏任务描述；轮询状态或结果分页时可传 `include_description=false` 减少重复传输。

## 凭据与本地数据

默认状态目录：`%LOCALAPPDATA%\CodexNativeAgentHub`。无 LOCALAPPDATA 时使用 `~/.local/share/CodexNativeAgentHub`。可通过 `AGENT_HUB_STATE_DIR` 覆盖；请保持在源码仓库外。

- `settings.json`：三套非密钥配置。
- `<代理>.dpapi`：Windows 当前用户加密的 Key。
- `jobs/`：任务摘要、原生会话及规范化活动。
- `jobs/<任务ID>/management.json`：独立保存归档状态和清理后的活动摘要，后台任务更新不会覆盖它。
- `storage-policy.json`：旧日志的保留天数，默认 30 天，可设 1–3650 天。
- `worktrees/`：保留的编辑工作区。

也支持 `AGENT_HUB_GEMINI_API_KEY`、`AGENT_HUB_CLAUDE_API_KEY`、`AGENT_HUB_DEEPSEEK_API_KEY`，环境变量优先于本地凭据。其他平台可用环境变量，但本项目的安装与原生集成测试范围为 Windows x64。

Key 经工作进程 stdin 传入，不放进启动参数、任务摘要或前端存储。面板活动只含固定标签和计数；**原生会话日志与最终结果可能包含任务、文件内容和路径**，不要上传状态目录。配置的模型服务会接收任务和代理读取的内容。本机面板 URL 自带随机访问令牌，请勿公开或转发。

「存储管理」按任务记录与结果、运行日志与会话缓存、编辑工作区、其他数据显示本机占用；统计只在打开页面或手动刷新时执行。数值为文件大小合计，链接、目录联接和无法读取的文件会跳过并提示统计不完整。

日志清理为手动操作：选择保留天数 → 预览任务及预计释放空间 → 点击「清理列出的运行日志」。预览有效期为 5 分钟，限用一次；更新保留天数后需重新预览。只清理结束时间和文件修改时间都早于期限的 SDK 数据（任务目录中的 `runtime/`、`session/`、`claude/`、`dsh/`、`activity.jsonl`、`dsh-profile.yml`）。运行中、结束时间不明确、预览后变更的文件及含不安全链接的目录会跳过。清理不会自动发生，也不取决于任务是否归档。

清理保留任务标题、描述、最终结果、活动摘要、全局连接配置、凭据及全部编辑工作区；任务目录中不在上述清理范围内的文件也不会删除。历史记录会继续积累，可随时查看；此功能主要控制通常占用更多空间的 SDK 日志与会话缓存。列表缓存只保留有数量上限的摘要，未变化的任务不会在每轮刷新时重新读取完整描述和结果。

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_*.py" -v
.\.venv\Scripts\python.exe -B tests/runtime_smoke.py
.\.venv\Scripts\python.exe -B scripts/audit_release.py
npm ci --ignore-scripts
npm test
npm run test:browser
```

单元测试覆盖独立凭据、配置快照、跨进程保存冲突、来源校验、对话隔离、归档恢复、列表及结果分页、日志清理的预览与路径保护、取消与超时、工具权限及 worktree 隔离。原生集成测试用真实 SDK 和运行组件连接本地模拟接口，验证三种代理的「读取 → 工具结果 → 回复」、文件写入及命令执行，检查实际进度与只读工具集；使用假 Key，不消耗模型服务额度。

前端开发检查需要 Node.js 24。`npm test` 用 jsdom 和延迟响应覆盖状态与请求竞态；`npm run test:browser` 默认使用本机 Edge，也可用 `BROWSER_EXECUTABLE` 指定 Chromium 系浏览器的可执行文件。浏览器检查覆盖 320、375、480、760、1024px 的四个页面、键盘与滚动交互，以及 axe 自动扫描；不替代人工读屏测试。模拟截图与检查记录保存在被 Git 忽略的 `outputs/validation/`。修复对应关系见 [审查修复清单](docs/review-fixes.md)。

GitHub Actions 在 Windows、Python 3.11 / 3.14 上执行后端检查，另设 Node.js 24 前端检查任务。模拟接口验证的是本项目与固定 SDK 的集成，不代表某个第三方中转服务已通过验证。Node.js 依赖只用于开发测试，运行面板不需要构建步骤。

欢迎通过 Issue 或 PR 提交最小复现和改进；请先运行上述检查，勿附带真实 Key、访问令牌、会话日志或个人路径。

## 参考与许可

- [Antigravity SDK](https://pypi.org/project/google-antigravity/)
- [Claude Agent SDK](https://platform.claude.com/docs/en/agent-sdk/python)
- [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
- [Codex MCP](https://developers.openai.com/codex/mcp/)

本项目从 [codex-gemini-antigravity](https://github.com/sdpd2333/codex-gemini-antigravity) 的本地面板与 MCP 桥接方案扩展而来，以独立仓库维护。项目代码采用 [MIT License](LICENSE)；第三方 SDK 及运行组件按各自条款分发。
