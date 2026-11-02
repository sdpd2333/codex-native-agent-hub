"""Keep edit jobs in separate Git worktrees; never commit, merge or discard user changes."""
from pathlib import Path
import subprocess

import common


def git(project: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-C", str(project), *args], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        raise common.RelayError("Git 操作失败；请检查 Git 安装和仓库状态。") from None
    if result.returncode:
        raise common.RelayError("Git 操作失败。请确认目录属于有效仓库，且至少有一次提交。")
    return result.stdout.strip()


def prepare(project: Path, job_id: str, backend: str, mode: str, workspace_mode: str) -> dict:
    if workspace_mode not in ("worktree", "direct"):
        raise common.RelayError("工作区模式必须为 worktree 或 direct。")
    if mode == "review" or workspace_mode == "direct":
        return {"path": str(project), "strategy": "direct", "source_project": str(project)}
    if workspace_mode != "worktree":
        raise common.RelayError("工作区模式必须为 worktree 或 direct。")
    root = Path(git(project, "rev-parse", "--show-toplevel")).resolve()
    if git(root, "status", "--porcelain"):
        raise common.RelayError("源仓库存在未提交更改。请先处理，或明确选择直接编辑模式。")
    base = git(root, "rev-parse", "HEAD")
    target = common.STATE_DIR / "worktrees" / job_id
    target.parent.mkdir(parents=True, exist_ok=True)
    branch = f"agent-hub/{backend}/{job_id}"
    git(root, "worktree", "add", "-b", branch, str(target), base)
    return {"path": str(target / project.relative_to(root)), "strategy": "worktree", "branch": branch,
            "root": str(target), "base_commit": base, "source_project": str(project)}


def changes(workspace: dict) -> dict:
    root = Path(workspace.get("root") or workspace["path"])
    base = workspace.get("base_commit", "HEAD")
    diff = git(root, "diff", "--no-ext-diff", "--no-textconv", base, "--")
    untracked = git(root, "ls-files", "--others", "--exclude-standard").splitlines()
    return {"workspace": workspace, "diff": diff[:100000], "diff_truncated": len(diff) > 100000,
            "untracked_files": untracked[:100], "note": "新增文件列在 untracked_files；未自动提交或合并。"}
