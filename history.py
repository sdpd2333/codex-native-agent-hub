"""Conversation views and reversible task archiving, independent of worker metadata."""
from __future__ import annotations

import re
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import secrets
import stat
import threading
import time
import common
import progress
import app_settings

_metadata: OrderedDict = OrderedDict()
_metadata_lock = threading.Lock()
PREVIEW_TTL = 300
_previews: dict[str, dict] = {}
_preview_lock = threading.Lock()


def validate_policy(enabled, after_days):
    if not isinstance(enabled, bool):
        raise common.RelayError("自动归档开关须为布尔值。")
    if isinstance(after_days, bool) or not isinstance(after_days, int) or not 1 <= after_days <= 3650:
        raise common.RelayError("自动归档天数须为 1 到 3650 之间的整数。")


def policy() -> dict:
    with common.configuration_lock():
        try:
            raw = (common.STATE_DIR / "archive-policy.json").read_bytes()
            data = json.loads(raw)
            enabled, days = data["enabled"], data["after_days"]
            validate_policy(enabled, days)
        except FileNotFoundError:
            raw, enabled, days = b"", True, 7
        except (OSError, ValueError, KeyError, TypeError, common.RelayError):
            raise common.RelayError("归档规则无法读取，请检查本地 archive-policy.json。") from None
        return {"enabled": enabled, "after_days": days, "revision": hashlib.sha256(raw).hexdigest()}


def save_policy(enabled, after_days, revision) -> dict:
    validate_policy(enabled, after_days)
    with common.configuration_lock():
        if revision != policy()["revision"]:
            raise common.ConfigurationConflict("归档规则已被其他面板更新，请重新加载规则后再保存。")
        common._atomic_write(common.STATE_DIR / "archive-policy.json",
                             json.dumps({"enabled": enabled, "after_days": after_days}).encode())
        return policy()


def safe_job(job_id: str):
    """Do not read or mutate a job reached through symlinks / Windows junctions."""
    directory = progress.job_dir(job_id)
    for path in (common.STATE_DIR, directory.parent, directory, directory / "job.json", directory / "management.json"):
        try:
            info = path.lstat()
        except FileNotFoundError:
            if path.name == "management.json":
                continue
            raise
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise OSError("linked job path")
        if not (stat.S_ISREG(info.st_mode) if path.name.endswith(".json") else stat.S_ISDIR(info.st_mode)):
            raise OSError("invalid job path")
        if path.name == "job.json":
            signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return signature


def eligible(job: dict, management: dict) -> bool:
    return (job.get("status") == "completed" and not management.get("archived_at")
            and not management.get("auto_archive_exempt"))


def aged(job: dict, cutoff: datetime) -> bool:
    try:
        finished = datetime.fromisoformat(job["finished_at"].replace("Z", "+00:00"))
        return finished.tzinfo is not None and finished <= cutoff
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


def auto_archive() -> int:
    saved = policy()
    if not saved["enabled"]:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=saved["after_days"])
    count = 0
    for candidate in records():
        job_id = candidate["job_id"]
        if not aged(candidate, cutoff) or not eligible(candidate, progress.load_management(job_id)):
            continue
        try:
            with common.configuration_lock():
                # A concurrent restore or policy change wins over this sweep's snapshot.
                if policy()["revision"] != saved["revision"]:
                    break
                safe_job(job_id)
                job = progress.load_job(job_id, load_external_result=False)
                if job and aged(job, cutoff) and eligible(job, progress.load_management(job_id)):
                    progress.update_management(job_id, archived_at=progress.now(), archive_reason="automatic")
                    count += 1
        except OSError:
            continue
    return count


def preview_legacy() -> dict:
    entries, summaries = [], []
    for candidate in records():
        job_id = candidate["job_id"]
        if candidate.get("conversation_id") or candidate.get("status") != "completed":
            continue
        try:
            with common.configuration_lock():
                signature = safe_job(job_id)
                job = progress.load_job(job_id, load_external_result=False)
                if job and not job.get("conversation_id") and eligible(job, progress.load_management(job_id)):
                    entries.append((job_id, signature))
                    summaries.append({"job_id": job_id, "title": progress.display_title(job),
                                      "finished_at": job.get("finished_at")})
        except OSError:
            continue
    token = secrets.token_urlsafe(24)
    with _preview_lock:
        for key in list(_previews):
            if _previews[key]["expires"] <= time.monotonic():
                del _previews[key]
        if len(_previews) >= 32:
            del _previews[next(iter(_previews))]
        _previews[token] = {"entries": entries, "root": str(common.STATE_DIR),
                            "expires": time.monotonic() + PREVIEW_TTL}
    return {"token": token, "expires_in": PREVIEW_TTL, "jobs": summaries, "job_count": len(entries)}


def archive_legacy(token) -> dict:
    if not isinstance(token, str):
        raise common.RelayError("请先预览未归属的旧任务。")
    with _preview_lock:
        plan = _previews.pop(token, None)
    if not plan or plan["root"] != str(common.STATE_DIR) or plan["expires"] <= time.monotonic():
        raise common.RelayError("归档预览已过期或已使用，请重新预览。")
    result = {"archived": 0, "skipped": 0, "errors": 0}
    for job_id, signature in plan["entries"]:
        try:
            with common.configuration_lock():
                if safe_job(job_id) != signature:
                    result["skipped"] += 1
                    continue
                job = progress.load_job(job_id, load_external_result=False)
                if not job or job.get("conversation_id") or not eligible(job, progress.load_management(job_id)):
                    result["skipped"] += 1
                    continue
                progress.update_management(job_id, archived_at=progress.now(), archive_reason="legacy")
                result["archived"] += 1
        except OSError:
            result["errors"] += 1
    return result


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
                signature = safe_job(path.parent.name)
            except OSError:
                continue
            key = str(path)
            with _metadata_lock:
                cached = _metadata.get(key)
                if cached:
                    _metadata.move_to_end(key)
            if cached and cached[0] == signature:
                job = cached[1]
            else:
                job = progress.load_job(path.parent.name, load_external_result=False)
                if job:
                    job["_display_title"] = progress.display_title(job)
                    job["title"] = job["_display_title"]
                    job["_result_chars"] = progress.result_chars(job)
                    job.pop("result", None)
                    job.pop("task_description", None)
                with _metadata_lock:
                    _metadata[key] = (signature, job)
                    if len(_metadata) > 4096:
                        _metadata.popitem(last=False)
            if job:
                yield dict(job)


def activity_time(job: dict) -> float:
    """Use completion time for terminal tasks, including long-running and legacy jobs."""
    for name in ("finished_at", "started_at"):
        try:
            value = datetime.fromisoformat(job[name].replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.timestamp()
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError, OSError):
            continue
    return float("-inf")


def query(*, conversation: str = "", view: str = "all", search: str = "", status: str = "all",
          offset: int = 0, limit: int = 50, live_jobs: tuple[dict, ...] = ()) -> dict:
    conversation = conversation_id(conversation)
    if view not in ("current", "all", "archived") or status not in ("all", "running", "completed", "attention"):
        raise common.RelayError("任务视图或状态筛选无效。")
    if not isinstance(search, str) or len(search) > 300:
        raise common.RelayError("搜索内容须为不超过 300 字符的文本。")
    if (any(isinstance(v, bool) or not isinstance(v, int) for v in (offset, limit))
            or offset < 0 or not 1 <= limit <= 100):
        raise common.RelayError("任务分页参数无效。")
    found, running, other_running = [], 0, 0
    statuses = {}
    archive_error = ""
    try:
        auto_archive()
    except (common.RelayError, OSError):
        archive_error = "自动归档暂不可用，请到设置 → 存储与归档检查配置。"
    needle = search.strip().casefold()
    merged = {job["job_id"]: job for job in records()}
    for job in live_jobs:
        merged[job["job_id"]] = job | {"_display_title": progress.display_title(job)}
    for job in merged.values():
        actual = progress.effective_status(job)
        same = bool(conversation) and job.get("conversation_id") == conversation
        if actual == "running":
            running += 1
            other_running += not same
        archived = bool(progress.load_management(job["job_id"]).get("archived_at"))
        if archived != (view == "archived") or view == "current" and not same:
            continue
        if status != "all" and not (actual == status or status == "attention" and actual in ("failed", "timed_out", "interrupted", "cancelled")):
            continue
        fields = [job["_display_title"], job["job_id"], job.get("backend"), job.get("model"),
                  job.get("project_dir"), job.get("conversation_id"), (job.get("workspace") or {}).get("path")]
        if needle and not any(needle in str(value or "").casefold() for value in fields):
            continue
        found.append(job)
        statuses[job["job_id"]] = actual
    found.sort(key=lambda job: (activity_time(job), job["job_id"]), reverse=True)
    recent_count = app_settings.values()["recent_completed_count"] if status == "all" and view != "archived" else 0
    recent_ids = [job["job_id"] for job in found if statuses[job["job_id"]] == "completed"][:recent_count]
    recent_set = set(recent_ids)
    found.sort(key=lambda job: 0 if statuses[job["job_id"]] == "running"
               else 1 if job["job_id"] in recent_set
               else 2 if statuses[job["job_id"]] != "completed" else 3)
    total = len(found)
    # Keep a valid last page after archive/filter changes.
    offset = min(offset, ((total - 1) // limit) * limit if total else 0)
    return {"jobs": [progress.snapshot(job, include_result=False) for job in found[offset:offset + limit]],
            "total": total, "offset": offset, "limit": limit,
            "recent_completed_ids": recent_ids,
            "next_offset": offset + limit if offset + limit < total else None,
            "running_count": running, "other_running_count": other_running, "archive_error": archive_error}


def archive(job_id: str, archived: bool) -> dict:
    if not isinstance(archived, bool):
        raise common.RelayError("archived 必须为布尔值。")
    with common.configuration_lock():
        try:
            safe_job(job_id)
        except OSError:
            raise common.RelayError("任务不存在或其存储路径不可安全访问。") from None
        job = progress.load_job(job_id, load_external_result=False)
        if job is None:
            raise common.RelayError("任务不存在。")
        if progress.effective_status(job) == "running":
            raise common.RelayError("运行中的任务不能归档，请等待结束。")
        if archived:
            progress.update_management(job_id, archived_at=progress.now(), archive_reason="manual")
        else:
            progress.update_management(job_id, archived_at=None, archive_reason=None,
                                       auto_archive_exempt=True, restored_at=progress.now())
    return {"job_id": job_id, "archived": archived}
