"""Codex MCP bridge and loopback panel for three native coding runtimes."""
from __future__ import annotations
import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import uuid
import webbrowser
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
import common
import dashboard
import history
import model_selection
import progress
import runtimes
import storage
import workspaces

JOBS: dict[str, dict] = {}
PANEL: dashboard.PanelServer | None = None
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
NETWORK_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def stop_process(process):
    if process is None or process.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill.exe", "/PID", str(process.pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        await killer.wait()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), 10)
    except TimeoutError:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        await process.wait()


@asynccontextmanager
async def lifespan(_server):
    try:
        yield {}
    finally:
        tasks = [job["future"] for job in JOBS.values() if not job["future"].done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        global PANEL
        if PANEL is not None:
            await asyncio.to_thread(PANEL.close)
            PANEL = None


mcp = FastMCP("native-agent-hub", lifespan=lifespan, instructions=
    "Delegate tasks to Gemini/Antigravity, Claude/Claude Code or DeepSeek/dsh using their native SDKs. "
    "Use get_configuration first; the user selects models and enters credentials in get_panel's local UI. "
    "Follow model_selection_policy for the chosen backend. If the user explicitly names a model, pass its exact ID "
    "as model AND quote the user's model instruction in model_override_reason. Naming providers, 'these three models', "
    "a previous task's model or a saved default is NOT an explicit model override. For pure connectivity/OK-only checks "
    "use task_kind=connectivity and omit model; the server fixes complexity to simple. Use submit_task for native SDK "
    "connectivity (including official client User-Agent requirements); test_connection is a direct HTTP probe. "
    "Provide a concise title separately from the full task. Otherwise, when auto_select_model is true, judge "
    "task_complexity as simple/standard/complex using "
    "the policy and submit it WITHOUT model; the server picks an exact live ID by model-name hints. When disabled, "
    "use the saved default unless the user explicitly requests another model. Never call select_model for automatic "
    "or one-task choices. Unknown names and catalog failures fall back to the saved default. "
    "Never request an API key in chat. Open the panel URL in a Codex right browser panel when available. "
    "Pass this chat's stable host conversation/thread ID as conversation_id to get_panel and every submit_task. "
    "If unavailable, call get_panel once without it and reuse the returned conversation_id within this chat. "
    "Never reuse IDs from other chats or infer them from project paths or server processes. "
    "submit_task returns immediately; poll get_job. Review is read-only. Edit permits file changes and commands "
    "only within the user's authorized task; it defaults to a separate clean Git worktree. "
    "Inspect get_changes and results. Changes are not automatically committed or merged.")


def profile_status(backend: str) -> dict:
    with common.configuration_lock():
        settings = common.load_settings(backend)
        configured = common.has_key(backend)
        revision = common.profile_revision(backend)
    return {**settings, **common.PROVIDERS[backend], "backend": backend, "key_configured": configured, "revision": revision,
            "model_selection_policy": model_selection.policy(settings["auto_select_model"], backend),
            "ready": configured and bool(settings["model"]) and bool(settings["base_url"]),
            "key_source": "environment" if os.environ.get("AGENT_HUB_" + backend.upper() + "_API_KEY", "").strip()
                          else "local" if configured else "none"}


@mcp.tool(annotations=READ)
def get_configuration(backend: Literal["gemini", "claude", "deepseek"] = "gemini") -> dict:
    """Read independent connection profiles and installed SDK versions. Never returns credentials."""
    common.check_backend(backend)
    return {**profile_status(backend), "profiles": {name: profile_status(name) for name in common.PROVIDERS},
            "runtimes": runtimes.doctor(), "default_project_dir": "",
            "configuration_launcher": str(common.APP_DIR / "configure.cmd"),
            "note": "Connection tests check generation. A task also verifies streaming and native tool compatibility."}


def ensure_panel() -> dashboard.PanelServer:
    global PANEL
    if PANEL is None:
        PANEL = dashboard.PanelServer(panel_dispatch, asyncio.get_running_loop()).start()
        common._atomic_write(common.STATE_DIR / "panel.json", json.dumps({"url": PANEL.url}).encode("utf-8"))
    return PANEL


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
async def get_panel(project_dir: str = "", conversation_id: str = "") -> dict:
    """Open a conversation-scoped local UI. Reuse the host chat ID for panel and task calls.

    If no host ID is available, reuse the generated conversation_id returned here within this chat.
    Calling without an ID creates a fresh view; history is still accessible under All tasks.
    """
    conversation_id = history.conversation_id(conversation_id) or uuid.uuid4().hex
    if project_dir:
        project = Path(project_dir)
        if not project.is_absolute() or not project.is_dir():
            raise common.RelayError("project_dir 必须是存在的绝对路径。")
        project_dir = str(project.resolve())
    return {"url": ensure_panel().register_context(conversation_id, project_dir),
            "conversation_id": conversation_id, "placement": "right", "runtimes": common.PROVIDERS}


def panel_credentials(payload: dict, backend: str) -> tuple[str, str, str]:
    base = common.normalize_base(str(payload.get("base_url", "")))
    entered = str(payload.get("key", "")).strip()
    if len(entered) > 8192:
        raise common.RelayError("API Key 长度超出范围。")
    if not entered and base != common.load_settings(backend)["base_url"]:
        raise common.RelayError("修改中转站地址时，请重新填写该站的 API Key。")
    return base, entered or common.load_key(backend), entered


async def panel_dispatch(method: str, action: str, payload: dict) -> dict:
    context = payload.get("_panel_context", {})
    conversation = context.get("conversation_id", "")
    if action == "jobs":
        return await asyncio.to_thread(history.query, conversation=conversation,
            view=payload.get("view", "current" if conversation else "all"), search=payload.get("search", ""),
            status=payload.get("status", "all"), offset=payload.get("offset", 0), limit=payload.get("limit", 50)) | {"updated_at": now()}
    if action == "archive":
        return await asyncio.to_thread(history.archive, payload.get("job_id", ""), payload.get("archived"))
    if action == "storage" and method == "GET":
        return await asyncio.to_thread(storage.overview)
    if action == "storage-policy":
        return await asyncio.to_thread(storage.save_policy, payload.get("retention_days"), payload.get("revision"))
    if action == "storage-preview":
        return await asyncio.to_thread(storage.preview, payload.get("retention_days"))
    if action == "storage-cleanup":
        return await asyncio.to_thread(storage.cleanup, payload.get("token"))
    if method == "GET":
        if action == "config":
            return get_configuration() | {"panel_context": context, "default_project_dir": context.get("project_dir", "")}
    backend = common.check_backend(str(payload.get("backend", "gemini")))
    if action in ("config", "models", "test"):
        key = ""
        try:
            with common.configuration_lock():
                if "revision" in payload:
                    common.require_revision(backend, payload["revision"])
                base, key, entered = panel_credentials(payload, backend)
                revision = common.profile_revision(backend)
            if action == "models":
                models = await asyncio.to_thread(common.fetch_models, base, key, backend)
                return {"backend": backend, "base_url": base, "revision": revision, "models": models, "total_count": len(models),
                        "selected": common.load_settings(backend)["model"]}
            profile = common.validate_profile(base, str(payload.get("model", "")).strip(), backend,
                                               str(payload.get("effort", "")))
            if action == "test":
                reply = await asyncio.to_thread(common.test_native_model, base, key, profile["model"], backend)
                return {"ok": True, "backend": backend, "reply": common.redact(reply, key),
                        "note": "测试不会保存配置。请先保存，再提交只读任务验证原生工具调用。"}
            # Validate the full profile before changing credentials for this backend.
            automatic = payload.get("auto_select_model", common.load_settings(backend)["auto_select_model"])
            if not isinstance(automatic, bool):
                raise common.RelayError("自动选择适配模型必须为布尔值。")
            with common.configuration_lock():
                common.require_revision(backend, payload.get("revision"))
                if entered:
                    common.save_key(entered, backend)
                common.save_settings(base, profile["model"], backend, profile["effort"], automatic)
            return {"ok": True, **get_configuration(backend), "panel_context": context,
                    "default_project_dir": context.get("project_dir", "")}
        except common.ConfigurationConflict:
            raise
        except Exception as exc:
            raise common.RelayError(common.redact(str(exc), key, str(payload.get("key", "")))) from None
    if action == "tasks":
        return await submit_task(payload.get("task"), payload.get("project_dir"), backend=backend,
            mode=payload.get("mode", "review"), model=payload.get("model") or None,
            timeout_seconds=payload.get("timeout_seconds", 600), workspace_mode=payload.get("workspace_mode", "worktree"),
            task_complexity=payload.get("task_complexity"), title=payload.get("title"),
            task_kind=payload.get("task_kind", "general"),
            conversation_id=conversation,
            model_override_reason="用户在新建任务面板中手动选择此型号。" if payload.get("model") else None)
    if action == "cancel":
        return await cancel_job(str(payload.get("job_id", "")))
    if action == "changes":
        return await get_changes(str(payload.get("job_id", "")))
    if action == "job":
        return await get_job(str(payload.get("job_id", "")), offset=payload.get("offset", 0),
                             max_chars=payload.get("max_chars", 20000),
                             include_description=payload.get("include_description", True))
    raise common.RelayError("接口不存在。")


@mcp.tool(annotations=NETWORK_READ)
async def list_models(backend: Literal["gemini", "claude", "deepseek"] = "gemini", query: str = "") -> dict:
    """Fetch exact available model IDs for one connection profile. Empty query returns all IDs."""
    settings = common.load_settings(backend)
    models = await asyncio.to_thread(common.fetch_models, settings["base_url"], common.load_key(backend), backend)
    matches = [model for model in models if query.casefold() in model.casefold()]
    return {"backend": backend, "models": matches, "matched_count": len(matches), "total_count": len(models),
            "selected": settings["model"], "model_candidates": model_selection.catalog(matches, backend),
            "model_selection_policy": model_selection.policy(settings["auto_select_model"], backend)}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
async def select_model(model: str, backend: Literal["gemini", "claude", "deepseek"] = "gemini") -> dict:
    """Save an exact model ID only when the user asks to change their selected model."""
    with common.configuration_lock():
        settings = common.load_settings(backend)
        revision = common.profile_revision(backend)
        key = common.load_key(backend)
    models = await asyncio.to_thread(common.fetch_models, settings["base_url"], key, backend)
    if model not in models:
        raise common.RelayError("模型不在当前 Key 可访问的列表中，请先调用 list_models。")
    with common.configuration_lock():
        common.require_revision(backend, revision)
        common.save_settings(settings["base_url"], model, backend, settings["effort"])
    return {"backend": backend, "selected": model}


@mcp.tool(annotations=NETWORK_READ)
async def test_connection(backend: Literal["gemini", "claude", "deepseek"] = "gemini") -> dict:
    """Direct HTTP generation probe using the saved model, not the official SDK client/UA.

    For native SDK connectivity use submit_task(task_kind="connectivity") without model.
    """
    settings = common.load_settings(backend)
    if not settings["model"]:
        raise common.RelayError("请先在面板选择并保存模型。")
    result = await asyncio.to_thread(common.test_native_model, settings["base_url"], common.load_key(backend), settings["model"], backend)
    return {"ok": True, "backend": backend, "model": settings["model"], "reply": result,
            "note": "Basic generation passed. A delegated review additionally checks SDK streaming and native tools."}


def worker_environment() -> dict:
    environment = {name: value for name, value in os.environ.items()
                   if not name.upper().endswith(("_API_KEY", "_AUTH_TOKEN"))
                   and not name.upper().startswith(("ANTHROPIC_", "CLAUDE_", "DSH_", "GEMINI_", "GOOGLE_API_"))}
    environment.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", AGENT_HUB_STATE_DIR=str(common.STATE_DIR.resolve()))
    return environment


async def execute(job: dict, task: str, timeout_seconds: int, profile: dict, key: str, workspace_mode: str):
    process = None
    try:
        async with asyncio.timeout(timeout_seconds):
            preparation = asyncio.create_task(asyncio.to_thread(workspaces.prepare, Path(job["project_dir"]),
                job["job_id"], job["backend"], job["mode"], workspace_mode))
            try:
                job["workspace"] = await asyncio.shield(preparation)
            except asyncio.CancelledError:
                # Git cannot be cancelled midway; preserve the created worktree for inspection.
                job["workspace"] = await preparation
                raise
            progress.save_job(job)
            directory = job["workspace"]["path"]
            run_dir = progress.job_dir(job["job_id"])
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
            process = await asyncio.create_subprocess_exec(
                sys.executable, str(common.APP_DIR / "agent_runner.py"), "--backend", job["backend"],
                "--project", directory, "--mode", job["mode"], "--run-dir", str(run_dir.resolve()),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=worker_environment(), cwd=directory, **options)
            job["process"] = process
            stdout, stderr = await process.communicate(json.dumps(
                {"task": task, "profile": profile, "key": key}, ensure_ascii=False).encode("utf-8"))
        try:
            result = json.loads(stdout.decode("utf-8"))
            if not isinstance(result, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            detail = common.redact(stderr.decode("utf-8", "replace"), key)[-2500:]
            raise common.RelayError("代理运行程序未正常返回 JSON。" + detail) from None
        if process.returncode != 0 or not result.get("ok"):
            raise common.RelayError(str(result.get("error", "代理运行程序失败。")))
        if not isinstance(result.get("text"), str) or not result["text"].strip():
            raise common.RelayError("代理未返回可见结果，任务未确认完成。请检查活动记录。")
        job.update(status="completed", result=common.redact(result["text"], key))
    except asyncio.CancelledError:
        await stop_process(process)
        job.update(status="cancelled")
    except TimeoutError:
        await stop_process(process)
        job.update(status="timed_out", error=f"任务超过 {timeout_seconds} 秒，已停止。")
    except Exception as exc:
        await stop_process(process)
        job.update(status="failed", error=common.redact(str(exc), key))
    finally:
        job["finished_at"] = now()
        progress.save_job(job)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def submit_task(task: str, project_dir: str, backend: Literal["gemini", "claude", "deepseek"] = "gemini",
                      mode: Literal["review", "edit"] = "review", model: str | None = None,
                      timeout_seconds: int = 600, workspace_mode: Literal["worktree", "direct"] = "worktree",
                      task_complexity: Literal["simple", "standard", "complex"] | None = None,
                      title: str | None = None, task_kind: Literal["general", "connectivity"] = "general",
                      model_override_reason: str | None = None, conversation_id: str = "") -> dict:
    """Delegate through a native runtime; return a job ID immediately. Poll get_job for results.

    Review only exposes read tools. Edit permits project changes and commands and defaults to a
    separate Git worktree (requires a clean committed source repository). Direct explicitly edits
    the given directory. Edits are kept for review and never automatically merged or discarded.

    Supply a short title (up to 80 characters); task retains the full description separately.
    Pass this chat's conversation_id from get_panel (or the host thread ID). Missing IDs stay unassigned.
    Pure connectivity/OK-only checks use task_kind=connectivity, mode=review and omit model.
    This fixes complexity to simple; actual debugging/review work uses task_kind=general.
    A model override requires model_override_reason quoting this task's explicit user model request.
    A provider name, saved default, previous task or "these three models" is not such a request.
    Explicit user model overrides win. Otherwise, when auto_select_model is enabled, Codex must
    judge task_complexity from model_selection_policy and omit model. The server selects from
    live IDs; saved defaults and reasoning effort stay unchanged. Disabled means use the default.
    """
    common.check_backend(backend)
    conversation_id = history.conversation_id(conversation_id)
    if not isinstance(task, str) or not task.strip() or len(task) > 100000:
        raise common.RelayError("任务须为 1 到 100000 字符的文本。")
    if not isinstance(project_dir, str):
        raise common.RelayError("请填写项目的绝对路径。")
    project = Path(project_dir)
    if not project.is_absolute() or not project.is_dir():
        raise common.RelayError("project_dir 必须是存在的绝对路径。")
    if mode not in ("review", "edit") or workspace_mode not in ("worktree", "direct"):
        raise common.RelayError("任务模式或工作区模式无效。")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 10 <= timeout_seconds <= 1800:
        raise common.RelayError("timeout_seconds 必须为 10 到 1800 之间的整数。")
    if task_complexity is not None and (not isinstance(task_complexity, str) or task_complexity not in model_selection.COMPLEXITIES):
        raise common.RelayError("task_complexity 必须为 simple、standard 或 complex。")
    if title is not None and (not isinstance(title, str) or len(title) > 80):
        raise common.RelayError("title 须为不超过 80 字符的简短标题；完整任务描述请放在 task。")
    if task_kind not in ("general", "connectivity"):
        raise common.RelayError("task_kind 必须为 general 或 connectivity。")
    if task_kind == "connectivity":
        if mode != "review":
            raise common.RelayError("纯连通性检测须使用 mode=review；实际修改或排错请使用 task_kind=general。")
        task_complexity = "simple"
    if model is not None:
        if (not isinstance(model_override_reason, str) or not model_override_reason.strip()
                or len(model_override_reason) > 300):
            raise common.RelayError("显式 model 须同时提供 model_override_reason（300 字符内），说明用户为本次任务指定型号的原话依据。"
                                    "不要复制默认模型或沿用上次型号；未明确指定时请省略 model，由自动选型处理。"
                                    "纯连通性检测请传 task_kind=connectivity。")
    elif model_override_reason is not None:
        raise common.RelayError("model_override_reason 仅用于用户明确指定 model 的本次覆盖。")
    with common.configuration_lock():
        settings = common.load_settings(backend)
        selected = settings["model"] if model is None else model
        if not isinstance(selected, str):
            raise common.RelayError("模型 ID 无效。")
        profile = common.validate_profile(settings["base_url"], selected, backend, settings["effort"])
        key = common.load_key(backend)
    if sum(job["status"] == "running" for job in JOBS.values()) >= 2:
        raise common.RelayError("已有两个任务运行中，请等待完成或取消其中一个。")
    selection = {"model": profile["model"], "mode": "explicit" if model is not None else "default",
                 "default_model": settings["model"],
                 "reason": "本次明确指定模型。" if model is not None else "自动选型已关闭，使用保存的默认模型。"}
    if model is not None:
        selection["override_reason"] = common.redact(model_override_reason.strip(), key)
        selection["reason"] = "用户指定型号：" + selection["override_reason"]
    if model is None and settings["auto_select_model"]:
        if task_complexity is None:
            raise common.RelayError("自动选型已开启：请由 Codex 判断任务复杂度并传入 task_complexity=simple/standard/complex。"
                                    "手动面板可在新建任务中选择复杂度；旧 MCP 客户端请重新加载服务。")
        try:
            models = await asyncio.to_thread(common.fetch_models, profile["base_url"], key, backend)
        except Exception:
            # Discovery failures must not leak upstream details or prevent use of the saved model.
            selection = model_selection.fallback(profile["model"], task_complexity,
                                                  "拉取模型列表失败，沿用默认模型。")
        else:
            selection = model_selection.choose_model(models, backend, profile["model"], task_complexity)
        profile["model"] = selection["model"]
        # Model discovery yields the event loop, so recheck the running-job limit.
        if sum(job["status"] == "running" for job in JOBS.values()) >= 2:
            raise common.RelayError("已有两个任务运行中，请等待完成或取消其中一个。")
    if len(JOBS) >= 50:
        for job_id, old in list(JOBS.items()):
            if old["status"] != "running":
                del JOBS[job_id]
                if len(JOBS) < 50:
                    break
    job_id = uuid.uuid4().hex[:16]
    panel = ensure_panel()
    description = common.redact(task, key)
    short_title = " ".join(title.split()) if title else ""
    job = {"job_id": job_id, "conversation_id": conversation_id, "status": "running", "backend": backend, "runtime": common.PROVIDERS[backend]["runtime"],
           "model": profile["model"], "mode": mode, "project_dir": str(project.resolve()), "started_at": now(),
           "model_selection": selection, "task_kind": task_kind,
           "title": common.redact(short_title, key) if short_title else (
               "原生 SDK 连通性检测" if task_kind == "connectivity" else progress.task_title(description, mode, str(project))),
           "task_description": description,
           "_pid": os.getpid(), "_control_url": panel.url}
    JOBS[job_id] = job
    progress.save_job(job)
    # Profile and key are immutable per job and never included in persisted job metadata.
    job["future"] = asyncio.create_task(execute(job, task, timeout_seconds, profile.copy(), key, workspace_mode))
    return {name: value for name, value in job.items() if name not in ("future", "task_description") and not name.startswith("_")} | {
        "dashboard_url": panel.register_context(conversation_id, str(project.resolve())) if conversation_id else panel.url}


@mcp.tool(annotations=READ)
async def get_job(job_id: str, wait_seconds: int = 0, offset: int = 0, max_chars: int = 20000,
                  include_description: bool = True) -> dict:
    """Read status, full task description and paginated result; optionally wait up to 20 seconds.

    Use include_description=false for result paging/status polling. Legacy description prefixes
    are marked task_description_complete=false; their lost text cannot be recovered here.
    """
    job = JOBS.get(job_id) or progress.load_job(job_id)
    if job is None:
        raise common.RelayError("任务不存在。")
    if (any(isinstance(value, bool) or not isinstance(value, int) for value in (wait_seconds, offset, max_chars))
            or not 0 <= wait_seconds <= 20 or offset < 0 or not 100 <= max_chars <= 50000
            or not isinstance(include_description, bool)):
        raise common.RelayError("查询参数超出范围。")
    if wait_seconds and job["status"] == "running" and "future" in job:
        await asyncio.wait({job["future"]}, timeout=wait_seconds)
    result = {name: value for name, value in job.items()
              if name not in ("future", "process", "result", "task_description") and not name.startswith("_")}
    activity = progress.snapshot(job, include_result=False)
    result.update(activity)
    if include_description:
        result.update(task_description=job.get("task_description", job.get("title", "")),
                      task_description_complete="task_description" in job)
    if "result" in job:
        full = job["result"]
        result.update(result=full[offset:offset + max_chars], offset=offset, total_chars=len(full),
                      next_offset=offset + max_chars if offset + max_chars < len(full) else None)
    return result


@mcp.tool(annotations=READ)
async def get_changes(job_id: str) -> dict:
    """Read a job's working diff and untracked file names. Does not commit or merge anything."""
    job = JOBS.get(job_id) or progress.load_job(job_id)
    if not job or not job.get("workspace"):
        raise common.RelayError("任务工作区尚未就绪。")
    return await asyncio.to_thread(workspaces.changes, job["workspace"])


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
async def cancel_job(job_id: str) -> dict:
    """Cancel a delegated job and terminate its native worker process tree; keep worktree edits."""
    if job_id not in JOBS:
        saved = progress.load_job(job_id)
        if saved is None:
            raise common.RelayError("任务不存在。")
        if saved.get("status") != "running":
            return {"job_id": job_id, "status": saved["status"]}
        if not progress.process_alive(saved.get("_pid", 0)):
            saved.update(status="interrupted", finished_at=now())
            progress.save_job(saved)
            return {"job_id": job_id, "status": "interrupted"}
        owner = saved.get("_control_url", "")
        if not owner or PANEL is not None and owner == PANEL.url:
            raise common.RelayError("任务不属于当前运行服务，无法取消。")
        try:
            return await asyncio.to_thread(dashboard.local_request, owner + "api/cancel", {"job_id": job_id}, timeout=20)
        except Exception:
            raise common.RelayError("无法连接任务所属服务，请稍后重试。") from None
    job = JOBS[job_id]
    if job["status"] == "running":
        job["future"].cancel()
        await asyncio.gather(job["future"], return_exceptions=True)
        if job["status"] == "running":
            job.update(status="cancelled", finished_at=now())
        progress.save_job(job)
    return {"job_id": job_id, "status": job["status"]}


async def run_panel(project_dir: str = "", open_browser: bool = True):
    existing = await asyncio.to_thread(dashboard.existing_panel)
    if existing:
        if open_browser:
            webbrowser.open(existing)
        elif sys.stdout is not None:
            print(json.dumps({"url": existing}), flush=True)
        return
    async with lifespan(mcp):
        info = await get_panel(project_dir)
        if open_browser:
            webbrowser.open(info["url"])
        elif sys.stdout is not None:
            print(json.dumps(info), flush=True)
        await asyncio.Event().wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--panel", action="store_true", help="Run the local web panel")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--project-dir", default="")
    args = parser.parse_args()
    if args.panel:
        asyncio.run(run_panel(args.project_dir, not args.no_browser))
    else:
        mcp.run(transport="stdio")
