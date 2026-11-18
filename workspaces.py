"""Keep edit jobs in separate Git worktrees; never commit, merge or discard user changes."""
from concurrent.futures import ThreadPoolExecutor
import io
import os
from pathlib import Path
import signal
import subprocess
from typing import BinaryIO

import common
import app_settings


CHUNK = 64 * 1024
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _scalar(stream: BinaryIO) -> str:
    return os.fsdecode(stream.read()).removesuffix("\n")  # Git plumbing terminates values with LF.


def _kill_tree(process) -> None:
    if os.name == "nt":
        # Git for Windows can start the real git.exe through a launcher. Its child
        # also owns the stdout pipe; killing just the launcher may never deliver EOF.
        try:
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"], timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        finally:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def git(project: Path, *args: str, read=_scalar, timeout: float | None = None):
    """Collect a command-specific stdout prefix, then drain and check Git under the same deadline."""
    if timeout is None:
        timeout = app_settings.values()["git_timeout_seconds"]
    try:
        with subprocess.Popen(["git", "-C", str(project), *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              creationflags=NO_WINDOW, start_new_session=os.name != "nt") as process:
            def collect():
                with process.stdout as stream:
                    value = read(stream)
                    # Keep draining without retaining the tail; a late Git error still matters.
                    while stream.read(CHUNK):
                        pass
                if process.wait():
                    raise common.RelayError("Git 操作失败。请确认目录属于有效仓库，且至少有一次提交。")
                return value

            with ThreadPoolExecutor(max_workers=1) as reader:
                future = reader.submit(collect)
                try:
                    return future.result(timeout)
                except TimeoutError:
                    _kill_tree(process)
                    raise common.RelayError("Git 操作超时。") from None
    except (OSError, subprocess.TimeoutExpired):
        raise common.RelayError("Git 操作失败；请检查 Git 安装和仓库状态。") from None


def prepare(project: Path, job_id: str, backend: str, mode: str, workspace_mode: str) -> dict:
    if workspace_mode not in ("worktree", "direct"):
        raise common.RelayError("工作区模式必须为 worktree 或 direct。")
    if mode == "review" or workspace_mode == "direct":
        return {"path": str(project), "strategy": "direct", "source_project": str(project)}
    timeout = app_settings.values()["git_timeout_seconds"]
    root = Path(git(project, "rev-parse", "--show-toplevel", timeout=timeout)).resolve()
    if git(root, "--no-optional-locks", "status", "--porcelain", "--untracked-files=normal",
           read=lambda stream: bool(stream.read(1)), timeout=timeout):
        raise common.RelayError("源仓库存在未提交更改。请先处理，或明确选择直接编辑模式。")
    base = git(root, "rev-parse", "HEAD", timeout=timeout)
    target = common.STATE_DIR / "worktrees" / job_id
    target.parent.mkdir(parents=True, exist_ok=True)
    branch = f"agent-hub/{backend}/{job_id}"
    git(root, "worktree", "add", "-b", branch, str(target), base, timeout=timeout)
    return {"path": str(target / project.relative_to(root)), "strategy": "worktree", "branch": branch,
            "root": str(target), "base_commit": base, "source_project": str(project)}


def _diff_preview(stream: BinaryIO, limit: int) -> tuple[str, bool]:
    text = io.TextIOWrapper(stream, encoding="utf-8", errors="replace", newline="\n")
    try:
        prefix = text.read(limit + 1)
    finally:
        text.detach()  # The caller drains the remaining bytes without decoding them.
    return prefix[:limit], len(prefix) > limit


def _untracked_preview(stream: BinaryIO, limit: int) -> tuple[list[str], bool]:
    names, tail = [], b""
    while block := stream.read(CHUNK):
        parts = (tail + block).split(b"\0")
        tail = parts.pop()
        for name in parts:
            if len(names) == limit:
                return names, True
            names.append(os.fsdecode(name))
    return names, False


def changes(workspace: dict) -> dict:
    root = Path(workspace.get("root") or workspace["path"])
    base = workspace.get("base_commit", "HEAD")
    settings = app_settings.values()
    timeout = settings["git_timeout_seconds"]
    diff, more_diff = git(root, "--no-optional-locks", "diff", "--no-ext-diff", "--no-textconv", base, "--",
                          read=lambda s: _diff_preview(s, settings["diff_max_chars"]), timeout=timeout)
    names, more_names = git(root, "--no-optional-locks", "ls-files", "-z", "--others", "--exclude-standard",
                            read=lambda s: _untracked_preview(s, settings["diff_max_files"]), timeout=timeout)
    return {"workspace": workspace, "diff": diff, "diff_truncated": more_diff,
            "untracked_files": names, "untracked_truncated": more_names,
            "note": "新增文件列在 untracked_files；未自动提交或合并。"}
