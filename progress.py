"""Persist job summaries and extract activity metadata, never model thoughts or tool output."""
from __future__ import annotations

from collections import OrderedDict, deque
from datetime import datetime, timezone
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import threading

import common
import app_settings
from events import read_activity

JOB_ID = re.compile(r"[0-9a-f]{16}\Z")
RESULT_FILE = re.compile(r"result-[0-9a-f]{64}\.txt\Z")
PUBLIC_FIELDS = ("job_id", "title", "task_description", "task_kind", "conversation_id", "backend", "runtime", "model", "model_selection", "mode", "project_dir", "workspace", "status", "started_at",
                 "finished_at", "result", "error", "runtime_settings", "timeout_seconds", "_pid", "_control_url", "_result_file", "_result_chars")
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
    if "result" in data:
        result = data.pop("result")
        encoded = result.encode("utf-8")
        # Publish the complete result before its metadata. A failed metadata write
        # leaves the previous result intact, including when a result is replaced.
        data["_result_file"] = "result-" + hashlib.sha256(encoded).hexdigest() + ".txt"
        data["_result_chars"] = len(result)
        common._atomic_write(job_dir(job["job_id"]) / data["_result_file"], encoded)
    common._atomic_write(job_dir(job["job_id"]) / "job.json",
                         json.dumps(data, ensure_ascii=False).encode("utf-8"))


def load_job(job_id: str, *, load_external_result: bool = True) -> dict | None:
    """Read old inline records too; list/paging callers can defer external result I/O."""
    path = job_dir(job_id) / "job.json"
    try:
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict) or data.get("job_id") != job_id:
            return None
        data = {key: data[key] for key in PUBLIC_FIELDS if key in data}
    except (OSError, ValueError):
        return None
    if load_external_result and "result" not in data and "_result_file" in data:
        data["result"] = read_result(data)
    return data


def result_chars(job: dict) -> int:
    return len(job["result"]) if "result" in job else job.get("_result_chars", 0)


# Only server-issued stream positions live here, never file handles or result text.
# A restart/eviction loses the fast path, not the validity of the character offset.
_page_positions: OrderedDict[str, int] = OrderedDict()
_page_lock = threading.Lock()


def _page_cursor(job_id: str, name: str, offset: int) -> str:
    return hashlib.sha256(f"{job_id}:{name}:{offset}".encode("ascii")).hexdigest()


def read_result(job: dict, offset: int = 0, max_chars: int | None = None) -> str:
    return result_page(job, offset, max_chars)["result"]


def result_page(job: dict, offset: int, max_chars: int | None, cursor: str | None = None) -> dict:
    total = result_chars(job)
    end = total if max_chars is None else min(offset + max_chars, total)
    page = {"offset": offset, "total_chars": total, "next_offset": end if end < total else None}
    if "result" in job:
        if cursor is not None:
            raise common.RelayError("结果分页游标无效或结果已更新，请重新加载。")
        return page | {"result": job["result"][offset:end]}
    name = job.get("_result_file", "")
    if not isinstance(name, str) or not RESULT_FILE.fullmatch(name):
        raise common.RelayError("任务结果文件引用无效。")
    job_id = job["job_id"]
    position = None
    if cursor is not None:
        if cursor != _page_cursor(job_id, name, offset):
            raise common.RelayError("结果分页游标无效或结果已更新，请重新加载。")
        with _page_lock:
            position = _page_positions.get(cursor)
    try:
        # No newline translation: CR, LF and astral characters keep their original offsets.
        with (job_dir(job_id) / name).open(encoding="utf-8", newline="\n") as stream:
            if position is not None:
                stream.seek(position)
            else:
                remaining = offset
                while remaining:
                    skipped = stream.read(min(remaining, 65536))
                    if not skipped:
                        break
                    remaining -= len(skipped)
            page["result"] = stream.read() if max_chars is None else stream.read(max_chars)
            if len(page["result"]) != max(0, end - offset):
                raise common.RelayError("任务结果文件不完整，请检查本地任务数据。")
            if end < total:
                key = _page_cursor(job_id, name, end)
                next_position = stream.tell()  # Seek cookie at an actual UTF-8 character boundary.
                with _page_lock:
                    _page_positions[key] = next_position
                    _page_positions.move_to_end(key)
                    if len(_page_positions) > 256:
                        _page_positions.popitem(last=False)
                page["next_cursor"] = key
        return page
    except (OSError, UnicodeError):
        raise common.RelayError("任务结果文件无法读取，请检查本地任务数据。") from None


MAX_MANAGEMENT_BYTES = 65536


def load_management(job_id: str, *, strict: bool = False) -> dict:
    path = job_dir(job_id) / "management.json"
    try:
        if path.stat().st_size > MAX_MANAGEMENT_BYTES:
            raise ValueError("oversized management sidecar")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("invalid management sidecar")
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        if strict:
            raise common.RelayError("任务管理记录无法读取，已保留原记录。") from None
        return {}


def update_management(job_id: str, **values) -> dict:
    # A sidecar prevents a running/older service's save_job from undoing an archive.
    with common.configuration_lock():
        data = load_management(job_id, strict=True) | values
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_MANAGEMENT_BYTES:
            raise common.RelayError("任务管理记录过大，已保留原记录。")
        common._atomic_write(job_dir(job_id) / "management.json", encoded)
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
        self.stat: os.stat_result | None = None
        self.offset = 0
        self.pending = b""
        self.dropping = False
        self.models: set = set()
        self.tools: dict = {}
        self.seen: set = set()
        # Keep enough events to increase the visible count without rereading a live log.
        self.events = deque(maxlen=app_settings.SCHEMA["activity_event_limit"]["max"])
        self.phase = "正在启动 Antigravity"
        self.last_activity_at = None

    def update(self, directory: Path, *, paths: list[Path] | None = None, backup: dict | None = None) -> dict:
        complete = paths is not None
        try:
            # The SDK stores its live JSONL under app_data_dir; save_dir may
            # contain only a session database. Keep the older layout supported.
            if paths is None:
                paths = list((directory / "runtime" / "brain").rglob("transcript.jsonl"))
                paths.extend((directory / "session").rglob("transcript.jsonl"))
            # After a partial cleanup an older transcript may still exist. It is
            # not a substitute for the source of the saved counters. Only compare
            # the saved name with discovered files; never open a sidecar-supplied path.
            if backup is not None:
                source = backup.get("_transcript")
                if not paths or (source and all(p.relative_to(directory).as_posix() != source for p in paths)):
                    return backup
            if paths:
                path = max(paths, key=lambda item: item.stat().st_mtime_ns)
                info = path.stat()
                size = info.st_size
                # A same-name replacement or in-place rewrite is not an append.
                old = self.stat
                if (old is None or path != self.path or not os.path.samestat(info, old) or size < old.st_size
                        or (size == old.st_size and info.st_mtime_ns != old.st_mtime_ns)):
                    self.__init__()
                    self.path = path
                self.stat = info
                with path.open("rb") as stream:
                    stream.seek(self.offset)
                    end = size if complete else min(size, self.offset + 2 * 1024 * 1024)
                    while self.offset < end:
                        chunk = stream.read(min(2 * 1024 * 1024, end - self.offset))
                        if not chunk:
                            raise OSError("transcript changed while reading")
                        self.offset = stream.tell()
                        self.consume(chunk)
                    if complete and self.pending:
                        self.consume(b"\n")  # A valid final JSON row need not end with a newline.
        except OSError:
            if complete:
                raise  # Cleanup must preserve logs when the final summary cannot be read.
            pass  # A transcript may be rotating or not yet created.
        activity = {"phase": self.phase, "model_calls": len(self.models), "tool_calls": sum(self.tools.values()),
                    "last_activity_at": self.last_activity_at, "events": list(self.events)}
        if complete:
            activity["_transcript"] = self.path.relative_to(directory).as_posix() if self.path else None
        return activity

    def consume(self, chunk: bytes) -> None:
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


def _job_activity(job: dict, management: dict, event_limit: int, *, log_paths: set[Path] | None = None) -> dict:
    backup = management.get("activity_summary")
    if not isinstance(backup, dict):
        backup = None
    directory = job_dir(job["job_id"])
    if job.get("backend", "gemini") != "gemini":
        path = directory / "activity.jsonl"
        return read_activity(path, event_limit, complete=log_paths is not None and path in log_paths, missing=backup)
    if backup is not None and "_transcript" not in backup and management.get("logs_cleaned_at"):
        return backup  # Older Gemini backups did not record which transcript was cleaned.
    with _tracker_lock:
        job_id = job["job_id"]
        if job_id not in _trackers:
            if len(_trackers) >= 60:
                _trackers.pop(next(iter(_trackers)))
            _trackers[job_id] = TranscriptTracker()
        paths = None if log_paths is None else [
            p for p in log_paths if p.name == "transcript.jsonl" and
            (p.is_relative_to(directory / "runtime" / "brain") or p.is_relative_to(directory / "session"))]
        return _trackers[job_id].update(directory, paths=paths, backup=backup)


def backup_activity(job: dict, log_paths: set[Path]) -> None:
    job_id = job["job_id"]
    summary = _job_activity(job, load_management(job_id, strict=True), app_settings.SCHEMA["activity_event_limit"]["max"],
                            log_paths=log_paths)
    update_management(job_id, activity_summary=summary)


def snapshot(job: dict, *, include_result: bool = True, preferences: dict | None = None) -> dict:
    job_id = job["job_id"]
    if preferences is None:
        preferences = app_settings.values()
    management = load_management(job_id)
    activity = _job_activity(job, management, preferences["activity_event_limit"]).copy()
    activity.pop("_transcript", None)
    # Legacy jobs were launched with the original budgets, not today's preferences.
    limits = job.get("runtime_settings") or app_settings.DEFAULTS
    activity["budget_model_calls"] = limits["max_model_calls"]
    activity["budget_tool_calls"] = limits["max_tool_calls"]
    activity["events"] = activity.get("events", [])[-preferences["activity_event_limit"]:]
    status = effective_status(job)
    if status != "running":
        activity["phase"] = FINAL_LABELS.get(status, status)
    data = {key: job[key] for key in ("job_id", "task_kind", "conversation_id", "backend", "runtime", "model", "model_selection", "mode", "project_dir", "workspace", "started_at", "finished_at", "error", "runtime_settings", "timeout_seconds", "persistence_error")
            if key in job}
    data.update(title=job.get("_display_title") or display_title(job), status=status, project_name=Path(job.get("project_dir", "")).name,
                progress=activity, can_cancel=status == "running" and bool(job.get("_control_url")),
                archived=bool(management.get("archived_at")), archived_at=management.get("archived_at"),
                archive_reason=management.get("archive_reason"), restored_at=management.get("restored_at"),
                auto_archive_exempt=bool(management.get("auto_archive_exempt")),
                logs_cleaned_at=management.get("logs_cleaned_at"))
    data["result_chars"] = result_chars(job)
    if include_result and ("result" in job or "_result_file" in job):
        data.update(result=read_result(job, max_chars=50000), result_truncated=data["result_chars"] > 50000)
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
        data = load_job(path.parent.name, load_external_result=False)
        if data:
            jobs.append(snapshot(data, include_result=include_result))
        if len(jobs) >= 50:
            break
    return sorted(jobs, key=lambda job: str(job.get("started_at", "")), reverse=True)
