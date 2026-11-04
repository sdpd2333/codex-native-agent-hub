"""Drive the packaged dsh runtime through its official Python SDK."""
import asyncio
import json
import app_settings
from runtimes.gemini import instructions


def make_config(project, mode, profile, key, run_dir):
    from deepseek_harness import DeepSeekHarnessConfig
    if mode == "review":
        patches = [{"id": "tool-fs", "config": {"read": True, "readImage": False, "write": False,
                                                "edit": False, "applyPatch": False}},
                   {"id": "persistent-pwsh", "disabled": True}, {"id": "persistent-bash", "disabled": True}]
    else:
        patches = [{"id": name, "disabled": True} for name in
                   ("tool-plugin-manager", "tool-subagent", "tool-subagent-fork", "tool-subagent-control")]
    patch_path = run_dir / "dsh-profile.yml"
    patch_path.write_text(json.dumps(patches), encoding="utf-8")
    return DeepSeekHarnessConfig(cwd=str(project), runtime_cwd=str(project),
        profile="sdk-minimal" if mode == "review" else "sdk", base_url=profile["base_url"].rstrip("/") + "/v1",
        api_key=key, model=profile["model"], reasoning_effort=profile.get("effort") or None,
        max_tokens=app_settings.task_values(profile)["deepseek_max_tokens"],
        patches=(str(patch_path),), dsh_home=str(run_dir / "dsh"),
        env={"DSH_TELEMETRY_DISABLED": "1", "DSH_MAX_TOKENS_AS_SUCCESS": "false",
             "DSH_SYSTEM_PROMPT": instructions(project, mode)})


async def run(task, project, mode, profile, key, run_dir, events):
    from deepseek_harness import DeepSeekHarness

    def execute():
        names = {}
        def notification(note):
            if note.method != "session.event":
                return
            event = note.payload.get("event", {})
            kind, data = event.get("type"), event.get("data", {})
            if not isinstance(data, dict):
                return
            if kind == "step/start":
                events.model(json.dumps([data.get("turn"), data.get("step")], sort_keys=True))
            elif kind == "tool/call":
                identity, name = data.get("callId"), data.get("name", "tool")
                names[identity] = name
                events.tool(identity, name)
            elif kind == "tool/result":
                message = data.get("message", {})
                identity = message.get("toolCallId") or data.get("callId")
                events.tool(identity, names.get(identity, "tool"), done=True, failed=bool(message.get("isError")))
        with DeepSeekHarness(make_config(project, mode, profile, key, run_dir)) as harness:
            session = harness.start_session()
            session.set_working_directory(str(project))
            result = session.run(instructions(project, mode) + "\n\n" + task, on_notification=notification)
            if result.finish_reason != "completed":
                raise RuntimeError("DeepSeek Harness 未完成任务：" + str(result.finish_reason))
            return result.final_response
    return await asyncio.to_thread(execute)
