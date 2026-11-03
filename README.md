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
```

也可在首次安装时加上 `-InstallSkill`。技能默认安装到 `~/.codex/skills/nah`（设置了 `CODEX_HOME` 时使用该目录下的 `skills/nah`）；若当前对话尚未识别，请重新加载技能或新开对话。

## 从右侧面板开始

重新加载 Codex 的 MCP 服务，或重新打开 Codex，然后说：

> 打开 native-agent-hub 的统一面板，放在右侧，使用当前项目和当前对话。

1. 在「连接设置」选择 Gemini、Claude 或 DeepSeek。
2. 填写该代理的 **Base URL 和 API Key**，点击「拉取模型」，选择完整模型 ID 后保存。
3. 点击「测试基础生成」确认连接。测试使用少量接口额度，**不会保存配置**；修改设置后需另行保存。
4. 在「新建任务」选择代理、项目目录和任务权限，填写目标。建议先提交「读取 README 并概括项目用途」这样的只读任务，验证工具调用。
5. 在「任务进展」查看活动与结果；编辑任务可点击「查看变更」，检查工作区路径和代码差异。

三套配置可使用同一中转站，也可使用不同服务，API Key 需分别填写；更换地址时需重新填写 Key。保存设置只影响之后提交的任务。

每个服务实例最多同时运行 2 个任务。任务默认最长运行 10 分钟，可在提交前调整，最多 30 分钟。

双击 `configure.cmd` 会在普通浏览器打开面板。也可手动启动：

```powershell
.\.venv\Scripts\python.exe -B server.py --panel --project-dir C:\Projects\my-app
```

升级后请重启 MCP 服务和独立面板，再从当前对话重新打开；服务重启后旧面板地址会失效。

## 自动选择适配模型

在「连接设置」为需要的代理开启「自动选择适配模型」（默认关闭）。开启后，根据任务复杂度从实时模型列表中选择型号：对话中由 Codex 判断复杂度，面板中由你选择「简单」「常规」或「复杂」。

你为本次任务明确指定的型号优先，临时选型不会修改默认配置。任务详情会显示实际模型与选择原因。选型按模型名称估算能力；无法匹配或拉取列表失败时，沿用已保存的默认模型。

## 中转站的协议要求

| 代理 | 服务商需要支持的接口 |
| --- | --- |
| Gemini | `/v1beta/models/*:generateContent`、`streamGenerateContent` 和函数调用 |
| Claude | Anthropic `/v1/messages`，支持 SSE、工具调用及 Claude Code 请求格式 |
| DeepSeek | **DeepSeek Messages `/v1/messages`**，支持 SSE 与工具调用 |

**当前 DeepSeek 运行环境使用 Messages 协议，只提供 OpenAI `/chat/completions` 的服务不够。** 请按服务商提供的兼容端点填写地址。

Base URL 使用 HTTPS，可保留服务商要求的路径前缀，末尾的 `/v1` 或 `/v1beta` 会自动处理。

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
- **归档与恢复**：默认在任务成功完成满 7 天后自动归档，可在「归档规则与旧任务整理」中调整或关闭，规则适用于本机所有对话。失败或取消的任务可手动归档；在「已归档」中恢复后，不再自动归档。
- **释放空间**：归档保留结果和工作区，不释放磁盘空间。进入「存储管理」，选择日志保留天数（默认 30 天），预览后手动清理；任务记录、最终结果、连接配置和编辑工作区都会保留。

配置、任务记录和工作区默认保存在 `%LOCALAPPDATA%\CodexNativeAgentHub`，API Key 使用 Windows 当前用户的 DPAPI 加密。

模型服务会接收任务及代理读取的内容。日志和结果可能包含文件内容与个人路径，请勿上传状态目录；面板地址带有访问令牌，请勿公开分享。

## 参考与许可

- [开发与验证](docs/development.md) · [更新日志](CHANGELOG.md) · [安全说明](SECURITY.md)
- [Antigravity SDK](https://pypi.org/project/google-antigravity/)
- [Claude Agent SDK](https://platform.claude.com/docs/en/agent-sdk/python)
- [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
- [Codex MCP](https://developers.openai.com/codex/mcp/)

本项目从 [codex-gemini-antigravity](https://github.com/sdpd2333/codex-gemini-antigravity) 的本地面板与 MCP 桥接方案扩展而来，以独立仓库维护。项目代码采用 [MIT License](LICENSE)；第三方 SDK 及运行组件按各自条款分发。
