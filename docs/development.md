# 开发与验证

先按 [README](../README.md) 安装项目依赖。前端开发检查需要 Node.js 24；Node.js 依赖只用于开发测试，运行面板不需要构建步骤。

在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_*.py" -v
.\.venv\Scripts\python.exe -B tests/runtime_smoke.py
.\.venv\Scripts\python.exe -B scripts/audit_release.py
npm ci --ignore-scripts
npm test
npm run test:browser
```

单元测试覆盖独立凭据、配置快照、跨进程保存冲突、来源校验、对话隔离、归档恢复、列表及结果分页、日志清理的预览与路径保护、取消与超时、工具权限及 worktree 隔离。原生集成测试用真实 SDK 和运行组件连接本地模拟接口，验证三种代理的「读取 → 工具结果 → 回复」、文件写入及命令执行，检查实际进度与只读工具集；使用假 Key，不消耗模型服务额度。

`npm test` 用 jsdom 和延迟响应覆盖状态与请求竞态；`npm run test:browser` 默认使用本机 Edge，也可用 `BROWSER_EXECUTABLE` 指定 Chromium 系浏览器的可执行文件。浏览器检查覆盖 320、375、480、760、1024px 的五个页面、键盘与滚动交互，以及 axe 自动扫描；不替代人工读屏测试。模拟截图与检查记录保存在被 Git 忽略的 `outputs/validation/`。修复对应关系见 [审查修复清单](review-fixes.md)。

GitHub Actions 在 Windows、Python 3.11 / 3.14 上执行后端检查，另设 Node.js 24 前端检查任务。模拟接口验证的是本项目与固定 SDK 的集成，不代表某个第三方中转服务已通过验证。

欢迎通过 Issue 或 PR 提交最小复现和改进；请先运行上述检查，勿附带真实 Key、访问令牌、会话日志或个人路径。

配置同步测试使用隔离的用户目录与假凭据，覆盖默认关闭、单次导入、跟随、字段冲突、来源变化、认证与协议识别；规则测试覆盖顺序、AND 条件、意图匹配、候选绑定、过期与配置版本、启动前回退及并发上限。前端测试覆盖规则草稿、同步选择和迟到试选响应。
