"""Read-only storage accounting and previewed cleanup of terminal jobs' SDK logs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
import stat
import threading
import time

import common
import history
import progress

LOG_DIRS = {"runtime", "session", "claude", "dsh"}
LOG_FILES = {"activity.jsonl", "dsh-profile.yml"}
TERMINAL = {"completed", "failed", "timed_out", "cancelled", "interrupted"}
PREVIEW_TTL = 300
_previews: dict[str, dict] = {}
_lock = threading.Lock()


def retention(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3650:
        raise common.RelayError("日志保留天数须为 1 到 3650 之间的整数。")
    return value


def policy() -> dict:
    with common.configuration_lock():
        try:
            raw = (common.STATE_DIR / "storage-policy.json").read_bytes()
            days = retention(json.loads(raw)["retention_days"])
        except FileNotFoundError:
            raw, days = b"", 30
        except (OSError, ValueError, KeyError, TypeError, common.RelayError):
            raise common.RelayError("存储策略无法读取，请检查本地配置文件。") from None
        return {"retention_days": days, "revision": hashlib.sha256(raw).hexdigest()}


def save_policy(days, revision) -> dict:
    days = retention(days)
    with common.configuration_lock():
        if revision != policy()["revision"]:
            raise common.ConfigurationConflict("存储策略已更新，请刷新统计后重新保存。")
        common._atomic_write(common.STATE_DIR / "storage-policy.json",
                             json.dumps({"retention_days": days}).encode())
        return policy()


def linked(info) -> bool:
    # Python 3.11 has no Path.is_junction(); all Windows reparse points are excluded.
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def safe_stat(path: Path):
    """Reject traversal through links, junctions or a replaced state root."""
    relative = path.relative_to(common.STATE_DIR)
    current = common.STATE_DIR
    info = current.lstat()
    if linked(info) or not stat.S_ISDIR(info.st_mode):
        raise OSError("unsafe state root")
    for part in relative.parts:
        if part in ("..", "."):
            raise OSError("unsafe path")
        current = current / part
        info = current.lstat()
        if linked(info):
            raise OSError("linked path")
    return info


def scan(root: Path) -> tuple[list, int]:
    files, skipped, pending = [], 0, [root]
    while pending:
        path = pending.pop()
        try:
            info = safe_stat(path)
            if stat.S_ISDIR(info.st_mode):
                pending.extend(path.iterdir())
            elif stat.S_ISREG(info.st_mode):
                files.append((path, info))
            else:
                skipped += 1
        except OSError:
            skipped += 1
    return files, skipped


def category(path: Path) -> str:
    parts = path.relative_to(common.STATE_DIR).parts
    if parts[0] == "worktrees":
        return "worktrees"
    if len(parts) >= 3 and parts[0] == "jobs" and progress.JOB_ID.fullmatch(parts[1]):
        if parts[2] in LOG_DIRS or parts[2] in LOG_FILES:
            return "logs"
        return "records"
    return "other"


def overview() -> dict:
    saved_policy = policy()
    files, skipped = scan(common.STATE_DIR)
    sizes = dict.fromkeys(("records", "logs", "worktrees", "other"), 0)
    for path, info in files:
        sizes[category(path)] += info.st_size
    return {"bytes": sizes, "total_bytes": sum(sizes.values()), "file_count": len(files),
            "job_count": sum(path.name == "job.json" and category(path) == "records" for path, _ in files),
            "skipped": skipped, "partial": bool(skipped), "policy": saved_policy}


def ended_before(job: dict, cutoff: datetime) -> bool:
    if job.get("status") not in TERMINAL:
        return False
    try:
        finished = datetime.fromisoformat(job["finished_at"].replace("Z", "+00:00"))
        return finished.tzinfo is not None and finished < cutoff
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def identity(info) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def candidates(job_id: str) -> tuple[list, int]:
    directory = progress.job_dir(job_id)
    files, skipped = [], 0
    for name in sorted(LOG_DIRS | LOG_FILES):
        path = directory / name
        try:
            info = safe_stat(path)
        except FileNotFoundError:
            continue
        except OSError:
            skipped += 1
            continue
        if name in LOG_DIRS and stat.S_ISDIR(info.st_mode):
            found, errors = scan(path)
            files.extend(found)
            skipped += errors
        elif name in LOG_FILES and stat.S_ISREG(info.st_mode):
            files.append((path, info))
        else:
            skipped += 1
    return files, skipped


def preview(days) -> dict:
    days = retention(days)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    manifest, summaries, skipped = [], [], 0
    for job in history.records():
        if not ended_before(job, cutoff):
            continue
        try:
            safe_stat(progress.job_dir(job["job_id"]) / "job.json")
            # A linked sidecar must never become a write destination during cleanup.
            sidecar = progress.job_dir(job["job_id"]) / "management.json"
            try:
                safe_stat(sidecar)
            except FileNotFoundError:
                pass
        except OSError:
            skipped += 1
            continue
        files, errors = candidates(job["job_id"])
        if errors:
            skipped += errors
            continue
        eligible = [(path, info) for path, info in files if info.st_mtime < cutoff.timestamp()]
        skipped += len(files) - len(eligible)
        if not eligible:
            continue
        entries = [(path.relative_to(common.STATE_DIR).as_posix(), identity(info)) for path, info in eligible]
        manifest.append({"job_id": job["job_id"], "finished_at": job["finished_at"], "files": entries})
        summaries.append({"job_id": job["job_id"], "title": job["_display_title"],
                          "finished_at": job["finished_at"], "bytes": sum(info.st_size for _, info in eligible),
                          "file_count": len(eligible)})
    token = secrets.token_urlsafe(24)
    with _lock:
        expired = [key for key, item in _previews.items() if item["expires"] <= time.monotonic()]
        for key in expired:
            del _previews[key]
        if len(_previews) >= 32:
            del _previews[next(iter(_previews))]
        _previews[token] = {"expires": time.monotonic() + PREVIEW_TTL, "root": str(common.STATE_DIR),
                            "cutoff": cutoff, "jobs": manifest}
    return {"token": token, "expires_in": PREVIEW_TTL, "retention_days": days, "jobs": summaries,
            "job_count": len(summaries), "file_count": sum(item["file_count"] for item in summaries),
            "bytes": sum(item["bytes"] for item in summaries), "skipped": skipped}


def cleanup(token) -> dict:
    if not isinstance(token, str):
        raise common.RelayError("请先预览要清理的运行日志。")
    with _lock:
        plan = _previews.pop(token, None)
    if not plan or plan["expires"] <= time.monotonic() or plan["root"] != str(common.STATE_DIR):
        raise common.RelayError("清理预览已过期或已使用，请重新预览。")
    result = {"deleted_files": 0, "freed_bytes": 0, "skipped_files": 0, "errors": 0, "jobs_cleaned": 0}
    for item in plan["jobs"]:
        job_id = item["job_id"]
        directory = progress.job_dir(job_id)
        try:
            safe_stat(directory / "job.json")
            job = progress.load_job(job_id)
            if not job or not ended_before(job, plan["cutoff"]) or job["finished_at"] != item["finished_at"]:
                result["skipped_files"] += len(item["files"])
                continue
            _, unsafe = candidates(job_id)
            if unsafe:
                result["skipped_files"] += len(item["files"])
                continue
            try:
                safe_stat(directory / "management.json")
            except FileNotFoundError:
                pass
            summary = progress.snapshot(job, include_result=False)["progress"]
            # Save counters before any deletion, including if the process later stops mid-cleanup.
            progress.update_management(job_id, activity_summary=summary, logs_cleaned_at=progress.now())
        except (OSError, common.RelayError):
            result["errors"] += 1
            result["skipped_files"] += len(item["files"])
            continue
        removed = 0
        for relative, original in item["files"]:
            path = common.STATE_DIR / relative
            try:
                info = safe_stat(path)
                # The server's single-use manifest contains exact paths, never client-supplied paths.
                if not stat.S_ISREG(info.st_mode) or identity(info) != original:
                    result["skipped_files"] += 1
                    continue
                path.unlink()
                result["deleted_files"] += 1
                result["freed_bytes"] += info.st_size
                removed += 1
            except FileNotFoundError:
                result["skipped_files"] += 1
            except OSError:
                result["errors"] += 1
                result["skipped_files"] += 1
        result["jobs_cleaned"] += bool(removed)
    return result
