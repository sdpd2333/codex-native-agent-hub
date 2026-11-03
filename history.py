"""Conversation views and reversible task archiving, independent of worker metadata."""
from __future__ import annotations

import re
from collections import OrderedDict
import threading
import common
import progress

_metadata: OrderedDict = OrderedDict()
_metadata_lock = threading.Lock()


def conversation_id(value: str | None) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise common.RelayError("conversation_id 须为不超过 128 字符的稳定对话标识，仅含字母、数字及 _ . : -。")
    return value


def records():
    for path in (common.STATE_DIR / "jobs").glob("*/job.json"):
        if progress.JOB_ID.fullmatch(path.parent.name):
            try:
                info = path.stat()
            except OSError:
                continue
            signature = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            key = str(path)
            with _metadata_lock:
                cached = _metadata.get(key)
                if cached:
                    _metadata.move_to_end(key)
            if cached and cached[0] == signature:
                job = cached[1]
            else:
                job = progress.load_job(path.parent.name)
                if job:
                    job["_display_title"] = progress.display_title(job)
                    job["title"] = job["_display_title"]
                    job["_result_chars"] = len(str(job.pop("result", "")))
                    job.pop("task_description", None)
                with _metadata_lock:
                    _metadata[key] = (signature, job)
                    if len(_metadata) > 4096:
                        _metadata.popitem(last=False)
            if job:
                yield dict(job)


def query(*, conversation: str = "", view: str = "all", search: str = "", status: str = "all",
          offset: int = 0, limit: int = 50) -> dict:
    conversation = conversation_id(conversation)
    if view not in ("current", "all", "archived") or status not in ("all", "running", "completed", "attention"):
        raise common.RelayError("任务视图或状态筛选无效。")
    if not isinstance(search, str) or len(search) > 300:
        raise common.RelayError("搜索内容须为不超过 300 字符的文本。")
    if (any(isinstance(v, bool) or not isinstance(v, int) for v in (offset, limit))
            or offset < 0 or not 1 <= limit <= 100):
        raise common.RelayError("任务分页参数无效。")
    found, running, other_running = [], 0, 0
    needle = search.strip().casefold()
    for job in records():
        actual = progress.effective_status(job)
        same = bool(conversation) and job.get("conversation_id") == conversation
        if actual == "running":
            running += 1
            other_running += not same
        archived = bool(progress.load_management(job["job_id"]).get("archived_at"))
        if archived != (view == "archived") or view == "current" and not same:
            continue
        if status != "all" and not (actual == status or status == "attention" and actual in ("failed", "timed_out", "interrupted")):
            continue
        fields = [job["_display_title"], job["job_id"], job.get("backend"), job.get("model"),
                  job.get("project_dir"), job.get("conversation_id"), (job.get("workspace") or {}).get("path")]
        if needle and not any(needle in str(value or "").casefold() for value in fields):
            continue
        found.append(job)
    found.sort(key=lambda job: str(job.get("started_at", "")), reverse=True)
    found.sort(key=lambda job: progress.effective_status(job) != "running")
    total = len(found)
    # Keep a valid last page after archive/filter changes.
    offset = min(offset, ((total - 1) // limit) * limit if total else 0)
    return {"jobs": [progress.snapshot(job, include_result=False) for job in found[offset:offset + limit]],
            "total": total, "offset": offset, "limit": limit,
            "next_offset": offset + limit if offset + limit < total else None,
            "running_count": running, "other_running_count": other_running}


def archive(job_id: str, archived: bool) -> dict:
    if not isinstance(archived, bool):
        raise common.RelayError("archived 必须为布尔值。")
    job = progress.load_job(job_id)
    if job is None:
        raise common.RelayError("任务不存在。")
    if progress.effective_status(job) == "running":
        raise common.RelayError("运行中的任务不能归档，请等待结束。")
    progress.update_management(job_id, archived_at=progress.now() if archived else None)
    return {"job_id": job_id, "archived": archived}
