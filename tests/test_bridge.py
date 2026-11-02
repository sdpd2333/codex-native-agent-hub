from __future__ import annotations
import asyncio
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import common
import server
from runtimes.gemini import make_config


class CommonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gemini-agent-test-")
        self.state = patch.object(common, "STATE_DIR", Path(self.temp.name))
        self.state.start()
        self.environment = patch.dict(os.environ, {"AGENT_HUB_" + name.upper() + "_API_KEY": "" for name in common.PROVIDERS})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.state.stop()
        self.temp.cleanup()

    def test_dpapi_and_settings(self):
        key = "local-test-not-a-real-key"
        common.save_key(key)
        self.assertEqual(common.load_key(), key)
        self.assertNotIn(key.encode(), (common.STATE_DIR / "gemini.dpapi").read_bytes())
        common.save_settings("https://relay.example.com/v1", "gemini-example")
        self.assertEqual(common.load_settings(), {"base_url": "https://relay.example.com", "model": "gemini-example", "effort": ""})
        self.assertNotIn(key, (common.STATE_DIR / "settings.json").read_text())

    def test_new_checkout_has_no_provider_or_model(self):
        self.assertEqual(common.load_settings(), {"base_url": "", "model": "", "effort": ""})
        self.assertFalse(common.has_key())
        self.assertNotEqual(common.default_state_dir(), common.APP_DIR)

    def test_model_ids_are_preserved(self):
        with patch.object(common, "request_json", return_value={"data": [{"id": "gemini-alias"}, {"id": "other"}, {"id": "gemini-alias"}]}):
            self.assertEqual(common.fetch_models("https://relay.example.com", "fake"), ["gemini-alias", "other"])

    def test_native_pagination_fallback(self):
        responses = [common.RelayError("not supported"),
                     {"models": [{"name": "models/gemini-b"}], "nextPageToken": "next"},
                     {"models": [{"name": "models/gemini-a"}]}]
        with patch.object(common, "request_json", side_effect=responses) as request:
            self.assertEqual(common.fetch_models("https://relay.example.com", "fake"), ["gemini-a", "gemini-b"])
            self.assertIn("pageToken=next", request.call_args.args[0])

    def test_http_errors_redact_key(self):
        key = "secret-test-token"
        error = HTTPError("https://relay.example.com/v1/models", 401, "error", {}, io.BytesIO(json.dumps({"error": {"message": key}}).encode()))
        with patch.object(common, "build_opener") as factory:
            factory.return_value.open.side_effect = error
            with self.assertRaises(common.RelayError) as result:
                common.request_json("https://relay.example.com/v1/models", key)
            self.assertNotIn(key, str(result.exception))
            self.assertIn("401", str(result.exception))
            error.close()

    def test_key_redaction_precedes_error_truncation(self):
        key = "long-secret-for-redaction"
        detail = "a" * 390 + key
        error = HTTPError("https://relay.example.com/v1/models", 401, "error", {}, io.BytesIO(json.dumps({"error": {"message": detail}}).encode()))
        with patch.object(common, "build_opener") as factory:
            factory.return_value.open.side_effect = error
            with self.assertRaises(common.RelayError) as result:
                common.request_json("https://relay.example.com/v1/models", key)
            self.assertNotIn(key[:10], str(result.exception))

    def test_base_url_validation(self):
        for invalid in ("http://relay.example.com", "https://key@relay.example.com", "https://relay.example.com?key=secret"):
            with self.assertRaises(common.RelayError):
                common.normalize_base(invalid)

    def test_cross_origin_redirect_is_rejected(self):
        from urllib.request import Request
        with self.assertRaises(common.RelayError):
            common._NoCrossOriginRedirect().redirect_request(Request("https://relay.example.com/v1/models"), None, 302, "", {}, "https://other.invalid/v1/models")

    def test_review_and_edit_capabilities(self):
        for mode in ("review", "edit"):
            config = make_config("https://relay.example.com", "fake", "gemini-alias", APP, mode, Path(self.temp.name).resolve())
            text = [target for target in config.models if any(t.value == "text" for t in target.types)]
            self.assertEqual(len(config.models), 1)
            from google.antigravity import Agent
            self.assertEqual(len(Agent(config)._config.models), 1)
            self.assertEqual(len(text), 1)
            self.assertEqual(text[0].name, "gemini-alias")
            self.assertEqual(text[0].endpoint.base_url, "https://relay.example.com")
            tools = {tool.value for tool in config.capabilities.enabled_tools}
            self.assertEqual("run_command" in tools, mode == "edit")
            self.assertEqual("edit_file" in tools, mode == "edit")
            self.assertNotIn("generate_image", tools)
            self.assertFalse(config.capabilities.enable_subagents)


class JobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        server.JOBS.clear()
        self.temp = tempfile.TemporaryDirectory(prefix="gemini-agent-test-")
        self.state = patch.object(common, "STATE_DIR", Path(self.temp.name))
        self.state.start()
        self.environment = patch.dict(os.environ, {"AGENT_HUB_GEMINI_API_KEY": "fake"})
        self.environment.start()
        common.save_settings("https://relay.example.com", "gemini-alias")

    async def asyncTearDown(self):
        for job in list(server.JOBS.values()):
            if not job["future"].done():
                await server.cancel_job(job["job_id"])
        if server.PANEL is not None:
            await asyncio.to_thread(server.PANEL.close)
            server.PANEL = None
        self.environment.stop()
        self.state.stop()
        self.temp.cleanup()

    async def test_missing_or_invalid_task_does_not_start(self):
        with self.assertRaises(common.RelayError):
            await server.submit_task("test", "relative/path")
        self.assertFalse(server.JOBS)

    async def test_result_and_pagination(self):
        class Process:
            returncode = 0
            async def communicate(self, payload):
                self.payload = json.loads(payload)
                return json.dumps({"ok": True, "text": "a" * 250}).encode(), b""
        async def create(*_args, **_kwargs):
            return Process()
        with patch.object(asyncio, "create_subprocess_exec", side_effect=create):
            job = await server.submit_task("review", str(APP))
            result = await server.get_job(job["job_id"], wait_seconds=1, max_chars=100)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(result["result"]), 100)
            self.assertEqual(result["next_offset"], 100)

    async def test_cancel_before_worker_starts(self):
        job = await server.submit_task("review", str(APP))
        self.assertEqual((await server.cancel_job(job["job_id"]))["status"], "cancelled")


async def mcp_smoke():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    with tempfile.TemporaryDirectory(prefix="gemini-agent-test-") as state:
        params = StdioServerParameters(command=sys.executable, args=[str(APP / "server.py")],
                                      env={**os.environ, "AGENT_HUB_STATE_DIR": state, "AGENT_HUB_GEMINI_API_KEY": "", "AGENT_HUB_CLAUDE_API_KEY": "", "AGENT_HUB_DEEPSEEK_API_KEY": "", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == {"get_configuration", "get_panel", "list_models", "select_model", "test_connection", "submit_task", "get_job", "cancel_job", "get_changes"}
                config = await session.call_tool("get_configuration", {})
                data = json.loads(config.content[0].text)
                assert not data["ready"] and not data["key_configured"]
                result = await session.call_tool("list_models", {})
                assert result.isError and "API Key" in result.content[0].text


class StdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_handshake_and_unconfigured_key(self):
        await mcp_smoke()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--mcp":
        asyncio.run(mcp_smoke())
    else:
        unittest.main()
