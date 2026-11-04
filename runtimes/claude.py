"""Claude Code through its official SDK, native hooks and stream events."""
from pathlib import Path
import app_settings
from runtimes.gemini import instructions

READ_TOOLS = ["Read", "Glob", "Grep"]
EDIT_TOOLS = READ_TOOLS + ["Write", "Edit", "Bash"]


def make_options(project, mode, profile, key, run_dir, events, failure=None):
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
    tools = EDIT_TOOLS if mode == "edit" else READ_TOOLS
    failure = {} if failure is None else failure

    async def before(data, call_id, _context):
        def stop(reason):
            # Hook exceptions alone are swallowed by the native client. Explicitly
            # deny this tool and stop the turn; the receiving loop reports failure.
            failure["error"] = reason
            return {"continue_": False, "stopReason": reason,
                    "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": reason}}

        if failure:
            return stop(failure["error"])
        name = data.get("tool_name", "")
        allowed = name in tools
        values = data.get("tool_input", {})
        if isinstance(values, dict):
            path = values.get("file_path") or values.get("path")
            if path and name != "Bash":
                resolved = Path(path)
                if not resolved.is_absolute():
                    resolved = project / resolved
                allowed = allowed and resolved.resolve().is_relative_to(project.resolve())
        if not allowed:
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": "Outside the assigned tool set or workspace."}}
        try:
            events.tool(call_id or data.get("tool_use_id", name), name)
        except RuntimeError as exc:
            return stop(str(exc))
        return {}

    async def after(data, call_id, _context):
        events.tool(call_id or data.get("tool_use_id", "tool"), data.get("tool_name", "tool"), done=True,
                    failed=data.get("hook_event_name") == "PostToolUseFailure")
        return {}

    env = {"ANTHROPIC_BASE_URL": profile["base_url"], "ANTHROPIC_API_KEY": key, "ANTHROPIC_AUTH_TOKEN": "",
           "CLAUDE_CONFIG_DIR": str(run_dir / "claude"), "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
           "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1", "CLAUDECODE": ""}
    kwargs = {"effort": profile["effort"]} if profile.get("effort") else {}
    return ClaudeAgentOptions(cwd=str(project), model=profile["model"], env=env, tools=tools, allowed_tools=tools,
        permission_mode="dontAsk", max_turns=app_settings.task_values(profile)["claude_max_turns"], setting_sources=[], strict_mcp_config=True,
        system_prompt={"type": "preset", "preset": "claude_code", "append": instructions(project, mode)},
        include_partial_messages=True, stderr=lambda _line: None,
        hooks={"PreToolUse": [HookMatcher(hooks=[before])], "PostToolUse": [HookMatcher(hooks=[after])],
               "PostToolUseFailure": [HookMatcher(hooks=[after])]}, **kwargs)


async def run(task, project, mode, profile, key, run_dir, events):
    from claude_agent_sdk import ClaudeSDKClient, StreamEvent, ResultMessage
    result = None
    failure = {}
    async with ClaudeSDKClient(options=make_options(project, mode, profile, key, run_dir, events, failure)) as client:
        await client.query(task)
        async for message in client.receive_response():
            if failure:
                raise RuntimeError(failure["error"])
            if isinstance(message, StreamEvent) and message.event.get("type") == "message_start":
                events.model(message.event.get("message", {}).get("id") or message.uuid)
            elif isinstance(message, ResultMessage):
                if message.is_error:
                    raise RuntimeError("Claude Code 未完成任务：" + str(message.subtype))
                result = message.result
    if failure:
        raise RuntimeError(failure["error"])
    return result or ""
