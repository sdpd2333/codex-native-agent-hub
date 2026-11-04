"""Persisted preferences, real adapter budgets and immutable running-job settings."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import app_settings
import common
import dashboard
from events import EventWriter, read_activity
import progress
import server
import workspaces
from runtimes import claude, deepseek, gemini


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-preferences-")
        self.addCleanup(self.temp.cleanup)
        self.state = patch.object(common, "STATE_DIR", Path(self.temp.name))
        self.state.start()
        self.addCleanup(self.state.stop)
        self.env = patch.dict(os.environ)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("AGENT_HUB_MAX_CONCURRENT_TASKS", None)

    def save(self, **values):
        return app_settings.save(values, app_settings.read()["revision"])

    def test_defaults_partial_save_and_cross_process_persistence(self):
        first = app_settings.read()
        self.assertEqual(first["values"], app_settings.DEFAULTS)
        common.save_settings("https://fixture.example.com", "fixture-model")
        saved = self.save(max_concurrent_tasks=3, task_timeout_seconds=3600)
        self.assertNotEqual(saved["revision"], first["revision"])
        self.assertEqual(common.load_settings()["model"], "fixture-model")
        environment = os.environ.copy()
        environment["AGENT_HUB_STATE_DIR"] = self.temp.name
        result = subprocess.run([sys.executable, "-B", "-c", "import app_settings,json; print(json.dumps(app_settings.values()))"],
                                cwd=APP, env=environment, capture_output=True, text=True, timeout=10,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), saved["values"])

    def test_revision_conflicts_and_write_failure_preserve_saved_values(self):
        first = self.save(max_concurrent_tasks=3)
        self.save(jobs_page_size=20)
        with self.assertRaises(common.ConfigurationConflict):
            app_settings.save({"max_concurrent_tasks": 4}, first["revision"])
        with patch.object(common, "_atomic_write", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                self.save(max_concurrent_tasks=4)
        self.assertEqual(app_settings.values()["max_concurrent_tasks"], 3)
        self.assertEqual(app_settings.values()["jobs_page_size"], 20)

    def test_all_fields_validate_types_bounds_and_enums_without_partial_writes(self):
        first = self.save(max_concurrent_tasks=3)
        for name, field in app_settings.SCHEMA.items():
            invalid = [None, True, [], {}, "unknown"]
            if "min" in field:
                invalid += [field["min"] - 1, field["max"] + 1, float(field["min"])]
            for value in invalid:
                with self.subTest(name=name, value=value), self.assertRaises(common.RelayError):
                    self.save(**{name: value})
        for value in (None, [], {"unknown": 2}):
            with self.assertRaises(common.RelayError):
                app_settings.save(value, first["revision"])
        self.assertEqual(app_settings.read()["revision"], first["revision"])

    def test_environment_override_is_visible_and_does_not_silently_accept_ineffective_save(self):
        self.save(max_concurrent_tasks=4)
        os.environ["AGENT_HUB_MAX_CONCURRENT_TASKS"] = "3"
        data = app_settings.read()
        self.assertEqual(data["values"]["max_concurrent_tasks"], 3)
        self.assertEqual(data["saved_values"]["max_concurrent_tasks"], 4)
        self.assertEqual(data["overrides"]["max_concurrent_tasks"]["value"], 3)
        with self.assertRaises(common.RelayError):
            self.save(max_concurrent_tasks=2)
        self.save(jobs_page_size=20)
        del os.environ["AGENT_HUB_MAX_CONCURRENT_TASKS"]
        self.assertEqual(server.max_concurrent_tasks(), 4)

    def test_adapters_and_event_enforcement_use_the_captured_budget(self):
        self.save(max_model_calls=3, max_tool_calls=4, claude_max_turns=7, deepseek_max_tokens=16384)
        captured = app_settings.task_values()
        profile = {"base_url": "https://fixture.example.com", "model": "fixture", "effort": "", "runtime_settings": captured}
        self.save(max_model_calls=40, deepseek_max_tokens=8192)
        root = Path(self.temp.name)
        events = EventWriter(root, captured)
        self.assertEqual(claude.make_options(root, "review", profile, "fixture", root, events).max_turns, 7)
        self.assertEqual(deepseek.make_config(root, "review", profile, "fixture", root).max_tokens, 16384)
        config = gemini.make_config(profile["base_url"], "fixture", "fixture", root, "review", root, captured)
        self.assertEqual(config.budget_config.max_model_calls, 3)
        self.assertEqual(config.budget_config.max_tool_calls, 4)
        for i in range(3):
            events.model(str(i))
        events.model("0")
        with self.assertRaisesRegex(RuntimeError, "3"):
            events.model("overflow")
        for i in range(4):
            events.tool(str(i), "Read")
        with self.assertRaisesRegex(RuntimeError, "4"):
            events.tool("overflow", "Read")

    def test_large_supported_budgets_keep_all_activity_counts(self):
        root = Path(self.temp.name)
        writer = EventWriter(root, {"max_model_calls": 1000, "max_tool_calls": 5000})
        for i in range(1100):
            writer.tool(str(i), "Read")
            writer.tool(str(i), "Read", done=True)
        activity = read_activity(root / "activity.jsonl", 50)
        self.assertEqual(activity["tool_calls"], 1100)
        self.assertEqual(len(activity["events"]), 50)

    def test_model_catalog_limit_never_returns_a_partial_success(self):
        self.save(model_catalog_max_pages=1, operation_timeout_seconds=35, network_timeout_seconds=10)
        with patch.object(common, "request_json", return_value={"data": [{"id": "a"}], "has_more": True, "last_id": "a"}):
            with self.assertRaisesRegex(common.RelayError, "后续页"):
                common.fetch_models("https://fixture.example.com", "fixture", "claude")
        self.save(model_catalog_max_pages=2)
        with patch.object(common, "request_json", side_effect=[
            {"data": [{"id": "a"}], "has_more": True, "last_id": "a"}, {"data": [{"id": "b"}]}]) as request:
            self.assertEqual(common.fetch_models("https://fixture.example.com", "fixture", "claude"), ["a", "b"])
            self.assertLessEqual(request.call_args.kwargs["timeout"], 35)

    def test_network_timeout_and_test_output_budget_reach_requests(self):
        self.save(network_timeout_seconds=12, connection_test_max_tokens=4096)
        with patch.object(common, "build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = b'{}'
            common.request_json("https://fixture.example.com", "fixture")
            self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 12)
        with patch.object(common, "request_json", return_value={"content": [{"type": "text", "text": "OK"}]}) as request:
            common.test_native_model("https://fixture.example.com", "fixture", "fixture", "claude")
            self.assertEqual(request.call_args.kwargs["payload"]["max_tokens"], 4096)

    def test_git_limits_and_visible_truncation(self):
        self.save(git_timeout_seconds=75, diff_max_chars=1000, diff_max_files=10)
        with patch.object(workspaces.subprocess, "run") as run:
            run.return_value.returncode, run.return_value.stdout = 0, "fixture"
            workspaces.git(Path(self.temp.name), "status")
            self.assertEqual(run.call_args.kwargs["timeout"], 75)
        with patch.object(workspaces, "git", side_effect=["x" * 1001, "\n".join(str(i) for i in range(11))]):
            result = workspaces.changes({"path": self.temp.name})
            self.assertEqual(len(result["diff"]), 1000)
            self.assertEqual(len(result["untracked_files"]), 10)
            self.assertTrue(result["diff_truncated"] and result["untracked_truncated"])


class LiveSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-live-settings-")
        self.state = patch.object(common, "STATE_DIR", Path(self.temp.name))
        self.state.start()
        self.env = patch.dict(os.environ, {"AGENT_HUB_CLAUDE_API_KEY": "settings-fixture"})
        self.env.start()
        os.environ.pop("AGENT_HUB_MAX_CONCURRENT_TASKS", None)
        common.save_settings("https://fixture.example.com", "fixture", "claude")
        server.JOBS.clear()
        progress._trackers.clear()
        self.panel = await server.get_panel(str(APP), "settings-fixture")

    async def asyncTearDown(self):
        for job in list(server.JOBS.values()):
            if not job["future"].done():
                await server.cancel_job(job["job_id"])
        await asyncio.to_thread(server.PANEL.close)
        server.PANEL = None
        server.JOBS.clear()
        self.env.stop()
        self.state.stop()
        self.temp.cleanup()

    async def test_panel_save_updates_live_admission_and_preserves_existing_job_budgets(self):
        url = self.panel["url"] + "api/app-settings"
        data = await asyncio.to_thread(dashboard.local_request, url)
        data = await asyncio.to_thread(dashboard.local_request, url, {"revision": data["revision"], "values": {
            "max_concurrent_tasks": 3, "task_timeout_seconds": 2700, "max_model_calls": 7, "result_page_chars": 100}})
        release, payloads = asyncio.Event(), []

        async def worker(job, task, timeout, profile, key, workspace):
            payloads.append((timeout, profile.copy()))
            await release.wait()
            job.update(status="completed", result="x" * 120)
            progress.save_job(job)

        with patch.object(server, "execute", new=worker):
            jobs = await asyncio.gather(*(server.submit_task("review", str(APP), backend="claude") for _ in range(3)))
            await asyncio.sleep(0)
            self.assertEqual(len(payloads), 3)
            self.assertTrue(all(timeout == 2700 and profile["runtime_settings"]["max_model_calls"] == 7 for timeout, profile in payloads))
            await asyncio.to_thread(dashboard.local_request, url, {"revision": data["revision"], "values": {
                "max_concurrent_tasks": 1, "task_timeout_seconds": 300, "max_model_calls": 9}})
            with self.assertRaises(common.RelayError):
                await server.submit_task("review", str(APP), backend="claude")
            self.assertTrue(all(job["status"] == "running" for job in server.JOBS.values()))
            release.set()
            await asyncio.gather(*(job["future"] for job in server.JOBS.values()))
            original = await server.get_job(jobs[0]["job_id"])
            self.assertEqual(original["progress"]["budget_model_calls"], 7)
            self.assertEqual(len(original["result"]), 100)
            self.assertEqual(original["next_offset"], 100)
            newer = await server.submit_task("review", str(APP), backend="claude", timeout_seconds=123)
            await server.JOBS[newer["job_id"]]["future"]
            self.assertEqual(payloads[-1][0], 123)
            self.assertEqual(payloads[-1][1]["runtime_settings"]["max_model_calls"], 9)


if __name__ == "__main__":
    unittest.main()
