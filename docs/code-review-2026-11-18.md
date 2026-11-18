# 代码审查与修复：日志清理、Git 工作区和轮询开销

2026-11-18，基线 `dev@5c97f5f`。以下 9 项已在工作区修复。优先保证删日志不丢统计，再降低 Git 预览、历史轮询和结果分页的开销；没有引入第三方依赖。

| 优先级 | 基线可复现的问题 | 修复与回归依据 |
|---|---|---|
| P1 | Claude、DeepSeek 的日志可删除但不可读时，清理返回成功，1 次模型和 1 次工具调用都变成 0。 | [活动读取](../events.py)区分可选日志缺失与最终读取失败。[清理测试](../tests/test_history_storage.py)覆盖三个后端：活动读取失败，该任务的活动日志、其他日志都保留。 |
| P2 | 预览后文件发生变化，实际删除 0 个，仍发布清理时间并冻结旧计数。 | [清理](../storage.py)先持久化摘要，再逐文件核对身份并删除，有实际删除才写清理时间。[进度读取](../progress.py)优先使用仍在的活动源；删除后即使清理时间未写入，也能从备份恢复。覆盖部分删除、归档状态保留、同名替换、删除后中断、重启及重试。 |
| P2 | 设置 `status.showUntrackedFiles=no` 后，带有未跟踪草稿的源仓库也获准创建 worktree，但新工作区看不到草稿。 | [准入检查](../workspaces.py)显式使用 `--untracked-files=normal`；忽略文件仍允许，直接编辑不受影响。[真实 Git 仓库回归](../tests/test_native_hub.py)。 |
| P2 | 前导空格丢失，中文文件名变成 Git 转义文本，路径中的 U+2028 会被当作换行。 | [变更预览](../workspaces.py)按 Git 的 `ls-files -z --others --exclude-standard` 解析 NUL 分隔字节。测试比较原文件名、空列表和预览前后的索引字节。 |
| P2 | `history.query` 先遍历目录归档，又遍历一次构建列表。 | [历史查询](../history.py)在一次查询中共用持久化记录和展示设置。保留独立后台归档，以及写入前在锁内复查配置、任务和还原豁免。[归档回归](../tests/test_archive_policy.py)包括首次查询即看到归档。 |
| P2 | 复制长结果时，每次从头解码到当前字符偏移。 | [结果分页](../progress.py)给落盘结果返回可选游标，绑定任务、内容哈希和字符偏移；只有服务生成的流位置可用于 `seek`。缓存淘汰或服务重启后仍可按字符定位。[面板](../dashboard.js)只推进成功页；[后端](../tests/test_result_storage.py)及[前端](../tests/dashboard.test.cjs)覆盖旧客户端、过期游标、Unicode、CRLF 和逐字复制。 |
| P2 | 预览上限 1,000 字符，旧实测却先把 575,105 字符的整个 diff 收进 Python。 | [Git 输出采集](../workspaces.py)只解码预览前缀，超出的字节分块排空，不保留完整补丁；未跟踪列表仅保留设置的条数。保留超时、退出码与截断标记。[实测与异常路径](../tests/test_app_settings.py)覆盖大量输出、输出后的非零退出和超时回收。 |
| P3 | 工作区模式入口已校验，`direct` 已返回，又重复检查一次不可能的模式。 | 删除[不可达判断](../workspaces.py)，保留对非法模式的入口报错。 |
| P3 | 自动选型返回后连续两次检查并发名额，配置读取的同步路径也有重复检查。 | [任务准入](../server.py)只保留联网前的快速拒绝、异步路由或查模型之后的登记前复查。[并发测试](../tests/test_model_selection.py)验证同时完成的模型目录请求不会超额。 |

## 实测

- **历史轮询：** 开启自动归档，连续查询 3 次，全目录枚举由 **6 次降为 3 次**。
- **顺序分页：** 混合中文、emoji 和 CRLF 的 100 万字符结果，每页 1 万字符。100 页拼接与原文逐字一致；文本流 `read()` 累计返回由 **5,050 万字符降为 100 万字符**。这不是物理磁盘读取量；旧客户端仍可按任意字符偏移跳页。
- **Git 预览：** 临时仓库生成 2,400,121 字节的补丁，限制为 1,000 个字符时，一次本地 `tracemalloc` 测得 Python 侧峰值 213,012 字节（约 208 KiB）。回归上限设为 512 KiB，不依赖具体机器的精确峰值。Git 子进程仍需生成输出，此优化针对宿主内存，而非承诺计算时间与补丁大小无关。

## 可借鉴的开源实现

链接固定到实际查看的提交；只提取与以上问题相关的做法，不复制上游实现或增加依赖。

| 上游与代码入口 | 可取之处 | 本项目不照搬的部分 |
|---|---|---|
| pre-commit v4.2.0 · [`get_all_files`](https://github.com/pre-commit/pre-commit/blob/aa48766b888990e7b118d12cf757109d96e65a7e/pre_commit/git.py#L154-L155) · [MIT](https://github.com/pre-commit/pre-commit/blob/aa48766b888990e7b118d12cf757109d96e65a7e/LICENSE) | 用 Git 的 `-z` 格式传递路径，而不是解析供人看的行。 | 上游此函数列索引中的文件；我们只需未跟踪且未被忽略的文件，无需引入 hook 框架。 |
| Claude Squad · [`GitWorktree.DiffNumstat`](https://github.com/smtg-ai/claude-squad/blob/070eb9387cda2be2e64aa1a32ee81be160c457a1/session/git/diff.go#L54-L74) · [AGPL-3.0](https://github.com/smtg-ai/claude-squad/blob/070eb9387cda2be2e64aa1a32ee81be160c457a1/LICENSE.md) | 同类多代理工作区把“只要增删数量”和“查看补丁正文”分开。 | 上游为纳入未跟踪文件调用 `git add -N`，会写索引。这里只借鉴分层，继续用 `ls-files` 只读列出新增文件。 |
| RQ v2.3.3 · [`should_run_maintenance_tasks`](https://github.com/rq/rq/blob/d5f6f57368389e25138a376799458c4be3d94815/rq/worker.py#L428-L435) · [BSD 许可条款](https://github.com/rq/rq/blob/d5f6f57368389e25138a376799458c4be3d94815/LICENSE) | 首次运行或维护间隔到期时才处理维护任务，适合作为归档扫描的后续优化。 | 使用已有维护循环与设置，不引入 Redis、RQ worker 或另一个调度器。 |
| MCP Python SDK v1.22.0 · [`list_resources_paginated`](https://github.com/modelcontextprotocol/python-sdk/blob/fcffa14b5b90d7b3fc7989a463a782334db9b453/examples/snippets/servers/pagination_example.py#L17-L38) · [MIT](https://github.com/modelcontextprotocol/python-sdk/blob/fcffa14b5b90d7b3fc7989a463a782334db9b453/LICENSE) | 服务端给出下一页游标，调用方无需推算位置；这是可复用的接口约定。 | 示例分页的是内存列表，不解决文件定位。[MCP 分页规范](https://modelcontextprotocol.io/specification/2025-11-25/server/utilities/pagination)针对列表操作，不要求改动本项目自定义 `get_job` 的现有接口。 |

## 验证范围

Windows，Python 3.14.8、Node.js 24.20.0。基线为 174 个 Python 和 54 个前端测试；修复后全量 **182 个 Python、54 个前端测试全部通过**。真实浏览器检查通过 45 个布局、26 项交互，axe 报告 0 个违规；三个原生 SDK 的 8 个本地模拟场景全部通过。发布审计和 `git diff --check` 通过。所有数据均为临时夹具，未调用真实模型服务。逐项回归与本地命令见[开发指南](development.md)。
