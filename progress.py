"""Persist job summaries and extract activity metadata, never model thoughts or tool output."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import threading

import common
from events import read_activity

JOB_ID = re.compile(r"[0-9a-f]{16}\Z")
PUBLIC_FIELDS = ("job_id", "title", "task_description", "task_kind", "conversation_id", "backend", "runtime", "model", "model_selection", "mode", "project_dir", "workspace", "status", "started_at",
                 "finished_at", "result", "error", "_pid", "_control_url")
TOOL_LABELS = {
    "view_file": "读取文件", "list_dir": "浏览目录", "search_dir": "搜索目录",
    "find_file": "查找文件", "find": "查找文件", "grep_search": "搜索代码",
    "create_file": "创建文件", "edit_file": "修改文件", "run_command": "执行命令",
    "finish": "整理结果",
}
FINAL_LABELS = {"completed": "任务完成", "failed": "任务失败", "timed_out": "任务超时",
                "cancelled": "任务已取消", "interrupted": "运行服务已退出"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def task_title(description: str, mode: str = "review", project_dir: str = "") -> str:
    """Use a complete short clause, or a neutral label; never truncate the task itself."""
    clause = re.split(r"[\r\n。！？!?；;]", description.strip(), maxsplit=1)[0].strip(" #*-\t")
    if 0 < len(clause) <= 60:
        return clause
    label = "编辑任务" if mode == "edit" else "只读审查"
    project = Path(project_dir).name
    return f"{label} · {project}" if project and len(project) <= 40 else label


def display_title(job: dict) -> str:
    # Older releases stored only a (sometimes truncated) prompt prefix as the title.
    # Keep that original record intact; expose it as incomplete description in get_job.
    if "task_description" in job and job.get("title"):
        return job["title"]
    return task_title(str(job.get("title", "")), job.get("mode", "review"), job.get("project_dir", ""))


def job_dir(job_id: str) -> Path:
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
        raise common.RelayError("任务 ID 无效。")
    return common.STATE_DIR / "jobs" / job_id


def save_job(job: dict) -> None:
    data = {key: job[key] for key in PUBLIC_FIELDS if key in job}
    common._atomic_write(job_dir(job["job_id"]) / "job.json",
                         json.dumps(data, ensure_ascii=False).encode("utf-8"))


def load_job(job_id: str) -> dict | None:
    path = job_dir(job_id) / "job.json"
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("job_id") != job_id:
            return None
        return {key: data[key] for key in PUBLIC_FIELDS if key in data}
    except (OSError, ValueError):
        return None


def load_management(job_id: str) -> dict:
    path = job_dir(job_id) / "management.json"
    try:
        if path.stat().st_size > 65536:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def update_management(job_id: str, **values) -> dict:
    # A sidecar prevents a running/older service's save_job from undoing an archive.
    with common.configuration_lock():
        data = load_management(job_id) | values
        common._atomic_write(job_dir(job_id) / "management.json", json.dumps(data, ensure_ascii=False).encode("utf-8"))
        return data


def effective_status(job: dict) -> str:
    status = job.get("status", "interrupted")
    if status == "running" and not process_alive(job.get("_pid", os.getpid())):
        return "interrupted"
    return status


def process_alive(pid: int) -> bool:
    try:
        if not isinstance(pid, int) or pid <= 0:
            return False
        if pid == os.getpid():
            return True
        if os.name == "nt":
            # On Windows os.kill(pid, 0) can terminate the process. Query its
            # exit code through a read-only handle instead.
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.GetExitCodeProcess.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return ctypes.get_last_error() == 5  # Access denied does not prove exit.
            try:
                code = wintypes.DWORD()
                if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, TypeError):
        return False


class TranscriptTracker:
    """Incremental bounded JSONL reader. Only retain static activity labels and counters."""

    def __init__(self):
        self.path: Path | None = None
        self.offset = 0
        self.pending = b""
        self.dropping = False
        self.models: set = set()
        self.tools: dict = {}
        self.seen: set = set()
        self.events = deque(maxlen=30)
        self.phase = "正在启动 Antigravity"
        self.last_activity_at = None

    def update(self, directory: Path) -> dict:
        try:
            # The SDK stores its live JSONL under app_data_dir; save_dir may
            # contain only a session database. Keep the older layout supported.
            paths = list((directory / "runtime" / "brain").rglob("transcript.jsonl"))
            paths.extend((directory / "session").rglob("transcript.jsonl"))
            if paths:
                path = max(paths, key=lambda item: item.stat().st_mtime_ns)
                size = path.stat().st_size
                if path != self.path or size < self.offset:
                    self.__init__()
                    self.path = path
                with path.open("rb") as stream:
                    stream.seek(self.offset)
                    chunk = stream.read(2 * 1024 * 1024)
                    self.offset = stream.tell()
                pieces = (self.pending + chunk).split(b"\n")
                self.pending = pieces.pop()
                for line in pieces:
                    if self.dropping:
                        self.dropping = False
                        continue
                    if len(line) > 1024 * 1024:
                        continue
                    try:
                        self.accept(json.loads(line))
                    except (ValueError, TypeError, AttributeError):
                        continue
                if len(self.pending) > 1024 * 1024:
                    self.pending = b""
                    self.dropping = True
        except OSError:
            pass  # A transcript may be rotating or not yet created.
        return {"phase": self.phase, "model_calls": len(self.models),
                "tool_calls": sum(self.tools.values()), "budget_model_calls": 40,
                "last_activity_at": self.last_activity_at, "events": list(self.events)}

    def accept(self, row: dict) -> None:
        kind = str(row.get("type", "")).lower()
        step = row.get("step_index")
        if not isinstance(step, int):
            return
        stamp = row.get("created_at")
        if not isinstance(stamp, str):
            stamp = None
        label = None
        if kind == "planner_response":
            self.models.add(step)
            calls = row.get("tool_calls", [])
            self.tools[step] = len(calls) if isinstance(calls, list) else 0
            label = "模型返回工具调用" if self.tools[step] else "模型生成回复"
            self.phase = "正在处理工具调用" if self.tools[step] else "正在生成回复"
        elif kind in TOOL_LABELS:
            label = TOOL_LABELS[kind]
            state = str(row.get("status", "")).upper()
            if state == "DONE":
                label += " · 已结束" if row.get("exit_code", 0) in (0, None) else " · 未成功"
            self.phase = label
        if label:
            self.last_activity_at = stamp or self.last_activity_at
            event_id = (step, kind, str(row.get("status", "")))
            if event_id not in self.seen:
                self.seen.add(event_id)
                self.events.append({"timestamp": stamp, "label": label})


_trackers: dict[str, TranscriptTracker] = {}
_tracker_lock = threading.RLock()


def snapshot(job: dict, *, include_result: bool = True) -> dict:
    with _tracker_lock:
        return _snapshot(job, include_result=include_result)


def _snapshot(job: dict, *, include_result: bool = True) -> dict:
    job_id = job["job_id"]
    management = load_management(job_id)
    if job_id not in _trackers:
        if len(_trackers) >= 60:
            _trackers.pop(next(iter(_trackers)))
        _trackers[job_id] = TranscriptTracker()
    activity = management.get("activity_summary") if management.get("logs_cleaned_at") else None
    if not isinstance(activity, dict):
        activity = (_trackers[job_id].update(job_dir(job_id)) if job.get("backend", "gemini") == "gemini"
                    else read_activity(job_dir(job_id) / "activity.jsonl"))
    activity = dict(activity)
    status = effective_status(job)
    if status != "running":
        activity["phase"] = FINAL_LABELS.get(status, status)
    data = {key: job[key] for key in ("job_id", "task_kind", "conversation_id", "backend", "runtime", "model", "model_selection", "mode", "project_dir", "workspace", "started_at", "finished_at", "error")
            if key in job}
    data.update(title=job.get("_display_title") or display_title(job), status=status, project_name=Path(job.get("project_dir", "")).name,
                progress=activity, can_cancel=status == "running" and bool(job.get("_control_url")),
                archived=bool(management.get("archived_at")), archived_at=management.get("archived_at"),
                logs_cleaned_at=management.get("logs_cleaned_at"))
    text = str(job.get("result", ""))
    data["result_chars"] = job.get("_result_chars", len(text))
    if include_result and "result" in job:
        data.update(result=text[:50000], result_truncated=len(text) > 50000)
    return data


def list_jobs(*, include_result: bool = True) -> list[dict]:
    directory = common.STATE_DIR / "jobs"
    try:
        paths = sorted(directory.glob("*/job.json"), key=lambda item: item.stat().st_mtime_ns, reverse=True)
    except OSError:
        return []
    jobs = []
    for path in paths:
        if not JOB_ID.fullmatch(path.parent.name):
            continue
        data = load_job(path.parent.name)
        if data:
            jobs.append(snapshot(data, include_result=include_result))
        if len(jobs) >= 50:
            break
    return sorted(jobs, key=lambda job: str(job.get("started_at", "")), reverse=True)
