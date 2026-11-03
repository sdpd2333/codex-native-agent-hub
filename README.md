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

Codex 调用 `get_panel(project_dir=项目绝对路径)`，再通过桌面应用的 `open_in_codex` 打开返回的 URL，设置 `target.type="browser"`、`placement="right"`。普通 MCP 客户端也可在浏览器打开同一面板。

1. 在「连接设置」选择 Gemini、Claude 或 DeepSeek。
2. 填写该代理使用的 **Base URL 和 API Key**，点击「拉取模型」，选择完整模型 ID 后保存。
3. 「测试基础生成」会发送一条简短请求，使用少量服务额度。
4. 在「新建任务」选择代理、项目、模式和目标，先用小型只读任务验证工具兼容性。
5. 在「任务进展」查看活动和结果；编辑任务可「查看变更」，读取 worktree 路径、分支与差异。

三套配置可以指向同一中转站，也可指向不同服务；Key 不会在代理之间自动复用。更换地址时必须重新填写 Key。任务提交时固定当时的配置，之后保存设置只影响新任务。

双击 `configure.cmd` 会在普通浏览器打开面板。也可手动启动：

```powershell
.\.venv\Scripts\python.exe -B server.py --panel --project-dir C:\Projects\my-app
```

加 `--no-browser` 只输出本机面板地址。服务结束后旧地址失效，重新获取即可。

## 自动选择适配模型

在「连接设置」开启「自动选择适配模型」（默认关闭）。Codex 判断任务复杂度，服务根据当前代理的**实时模型列表**选型。

- **Gemini**：按版本号、能力从低到高排序。版本号优先；同版本 Flash < Pro / Ultra / Argon。同版本同能力合为一组，按以下位置选择。
- **Claude**：Haiku 为快速、Sonnet 为均衡、Opus / Fable 为高能力。按下表选档，同档优先较新版本，同版本优先稳定版。
- **DeepSeek**：在 Flash 型号中选择，同档优先稳定版和较新版本。

| 任务复杂度 | 典型任务 | Gemini 实时梯度位置 | Claude 档位顺序 |
| --- | --- | --- | --- |
| `simple` | 文案、样式、局部小修、只读摘要 | 低位 | 轻量 → 快速 → 均衡 |
| `standard` | 常规功能、测试、排错 | 中位（向下取整） | 均衡 → 快速 → 高能力 |
| `complex` | 架构、困难排错、大范围重构 | 高位 | 高能力 |

Gemini 组内优先稳定版、旗舰系列及已保存模型。

候选限于当前代理可识别的通用编码型号，保留原始 ID。无法匹配或列表失败时沿用默认模型。规则按名称估算能力。

手动指定模型优先，自动选型仅影响本次任务。对话中由 Codex 判断复杂度，面板新建任务时由用户选择；任务详情显示实际模型与选型原因。

MCP 调用：`get_configuration` 返回 `model_selection_policy`，`list_models` 返回名称分类。自动选型时向 `submit_task` 传入 `task_complexity` 并省略 `model`；手动指定时传入完整 `model`。

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
| `get_panel(project_dir)` | 获取本地统一面板地址，可预填项目路径 |
| `list_models(backend, query)` | 拉取完整模型 ID，空查询返回所有模型 |
| `select_model(model, backend)` | 按用户要求保存默认模型 |
| `test_connection(backend)` | 用保存的配置测试原生文本生成，会使用接口额度 |
| `submit_task(...)` | 提交任务，立即返回任务 ID 和面板地址 |
| `get_job(job_id, ...)` | 查询状态、活动与结果，支持等待和结果分页 |
| `get_changes(job_id)` | 读取工作区差异和新增文件名 |
| `cancel_job(job_id)` | 终止本机工作进程树，保留文件修改 |

`submit_task` 参数包括 `task`、绝对路径 `project_dir`、`backend=gemini|claude|deepseek`、`mode=review|edit`、可选 `model`、可选 `task_complexity=simple|standard|complex`、`workspace_mode=worktree|direct`、`timeout_seconds`。默认 Gemini、review、worktree，超时 600 秒（可设 10–1800 秒）。

## 凭据与本地数据

默认状态目录：`%LOCALAPPDATA%\CodexNativeAgentHub`。无 LOCALAPPDATA 时使用 `~/.local/share/CodexNativeAgentHub`。可通过 `AGENT_HUB_STATE_DIR` 覆盖；请保持在源码仓库外。

- `settings.json`：三套非密钥配置。
- `<代理>.dpapi`：Windows 当前用户加密的 Key。
- `jobs/`：任务摘要、原生会话及规范化活动。
- `worktrees/`：保留的编辑工作区。

也支持 `AGENT_HUB_GEMINI_API_KEY`、`AGENT_HUB_CLAUDE_API_KEY`、`AGENT_HUB_DEEPSEEK_API_KEY`，环境变量优先于本地凭据。其他平台可用环境变量，但本项目的安装与原生集成测试范围为 Windows x64。

Key 经工作进程 stdin 传入，不放进启动参数、任务摘要或前端存储。面板活动只含固定标签和计数；**原生会话日志与最终结果可能包含任务、文件内容和路径**，不要上传状态目录。配置的模型服务会接收任务和代理读取的内容。本机面板 URL 自带随机访问令牌，请勿公开或转发。

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_*.py" -v
.\.venv\Scripts\python.exe -B tests/runtime_smoke.py
.\.venv\Scripts\python.exe -B scripts/audit_release.py
```

单元测试覆盖独立凭据、配置快照、来源校验、结果持久化、取消与超时、工具权限及 worktree 隔离。原生集成测试用真实 SDK 和运行组件连接本地模拟接口，验证三种代理的「读取 → 工具结果 → 回复」、文件写入及命令执行，检查实际进度与只读工具集；使用假 Key，不消耗模型服务额度。

GitHub Actions 在 Windows、Python 3.11 / 3.14 上执行这些检查。模拟接口验证的是本项目与固定 SDK 的集成，不代表某个第三方中转服务已通过验证。

欢迎通过 Issue 或 PR 提交最小复现和改进；请先运行上述检查，勿附带真实 Key、访问令牌、会话日志或个人路径。

## 参考与许可

- [Antigravity SDK](https://pypi.org/project/google-antigravity/)
- [Claude Agent SDK](https://platform.claude.com/docs/en/agent-sdk/python)
- [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
- [Codex MCP](https://developers.openai.com/codex/mcp/)

本项目从 [codex-gemini-antigravity](https://github.com/sdpd2333/codex-gemini-antigravity) 的本地面板与 MCP 桥接方案扩展而来，以独立仓库维护。项目代码采用 [MIT License](LICENSE)；第三方 SDK 及运行组件按各自条款分发。
