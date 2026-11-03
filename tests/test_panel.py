from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import common
import dashboard
import progress
import server


class ActivityTests(unittest.TestCase):
    def test_process_probe_never_terminates_a_running_worker(self):
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
        try:
            self.assertTrue(progress.process_alive(os.getpid()))
            self.assertTrue(progress.process_alive(process.pid))
            self.assertIsNone(process.poll())
        finally:
            process.terminate()
            process.wait(timeout=5)
        self.assertFalse(progress.process_alive(process.pid))

    def test_incremental_activity_excludes_content_and_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "session" / "run" / "transcript.jsonl"
            path.parent.mkdir(parents=True)
            model = {"step_index": 1, "type": "PLANNER_RESPONSE", "status": "DONE",
                     "created_at": "2026-01-01T00:00:00Z", "content": "private-thought",
                     "tool_calls": [{"name": "view_file", "args": {"path": "private-file"}}]}
            tool = {"step_index": 2, "type": "VIEW_FILE", "status": "DONE",
                    "created_at": "2026-01-01T00:00:01Z", "content": "private-file-contents"}
            encoded = (json.dumps(tool) + "\n").encode()
            path.write_bytes((json.dumps(model) + "\n").encode() + encoded[:15])
            tracker = progress.TranscriptTracker()
            first = tracker.update(root)
            self.assertEqual((first["model_calls"], first["tool_calls"]), (1, 1))
            with path.open("ab") as stream:
                stream.write(encoded[15:])
            second = tracker.update(root)
            self.assertIn("读取文件", second["phase"])
            self.assertEqual(len(second["events"]), 2)
            self.assertEqual(tracker.update(root), second)
            self.assertNotIn("private-", json.dumps(second))

    def test_record_updates_do_not_double_count_calls(self):
        tracker = progress.TranscriptTracker()
        row = {"step_index": 1, "type": "PLANNER_RESPONSE", "status": "RUNNING", "tool_calls": []}
        tracker.accept(row)
        tracker.accept({**row, "status": "DONE", "tool_calls": [{"name": "finish"}]})
        tracker.accept({**row, "status": "DONE", "tool_calls": [{"name": "finish"}]})
        self.assertEqual(len(tracker.models), 1)
        self.assertEqual(sum(tracker.tools.values()), 1)


class PanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gemini-panel-test-")
        self.state = patch.object(common, "STATE_DIR", Path(self.temp.name))
        self.state.start()
        self.environment = patch.dict(os.environ, {"AGENT_HUB_" + name.upper() + "_API_KEY": "" for name in common.PROVIDERS})
        self.environment.start()
        server.JOBS.clear()
        progress._trackers.clear()
        self.info = await server.get_panel(str(APP))
        self.url = self.info["url"]

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

    async def request(self, action="", method="GET", payload=None, headers=None, url=None):
        def call():
            body = None if payload is None else json.dumps(payload).encode()
            supplied = {"Content-Type": "application/json", "X-Agent-Hub": "1"}
            supplied.update(headers or {})
            req = Request(url or self.url + action, data=body, method=method, headers=supplied)
            try:
                with build_opener(ProxyHandler({})).open(req, timeout=5) as response:
                    return response.status, response.read(), dict(response.headers)
            except HTTPError as exc:
                try:
                    return exc.code, exc.read(), dict(exc.headers)
                finally:
                    exc.close()
        return await asyncio.to_thread(call)

    async def test_unconfigured_panel_and_access_boundaries(self):
        status, html, headers = await self.request()
        self.assertEqual(status, 200)
        self.assertIn(b"Native Agent Hub", html)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        status, script, headers = await self.request("dashboard.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["Content-Type"])
        self.assertIn(b"use strict", script)
        status, body, _ = await self.request("api/config")
        config = json.loads(body)
        self.assertFalse(config["ready"])
        self.assertNotIn("key", config)
        self.assertEqual(config["default_project_dir"], str(APP))
        root = "http://" + server.PANEL.host
        self.assertEqual((await self.request(url=root + "/wrong/api/config"))[0], 404)
        self.assertEqual((await self.request("api/config", headers={"Host": "untrusted.invalid"}))[0], 403)
        self.assertEqual((await self.request("api/config", headers={"Host": "localhost"}))[0], 403)
        for extra, expected in (({"X-Agent-Hub": ""}, 403),
                                ({"Origin": "https://untrusted.invalid"}, 403),
                                ({"Content-Type": "text/plain"}, 415)):
            with self.subTest(headers=extra):
                self.assertEqual((await self.request("api/config", "POST", {}, extra))[0], expected)
        self.assertEqual((await self.request("api/tasks"))[0], 404)
        self.assertEqual((await self.request("api/config", "POST", []))[0], 400)

    async def test_configuration_round_trip_and_saved_key_is_not_returned(self):
        key = "panel-fixture-credential"
        payload = {"base_url": "https://relay.example.com/v1", "key": key, "model": "gemini-alias", "auto_select_model": True,
                   "revision": common.profile_revision("gemini")}
        status, body, _ = await self.request("api/config", "POST", payload)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ready"])
        self.assertTrue(json.loads(body)["auto_select_model"])
        self.assertNotIn(key.encode(), body)
        self.assertEqual(common.load_key(), key)
        self.assertEqual(common.load_settings()["base_url"], "https://relay.example.com")
        status, body, _ = await self.request("api/config")
        self.assertEqual(status, 200)
        self.assertNotIn(key.encode(), body)
        self.assertTrue(json.loads(body)["profiles"]["gemini"]["auto_select_model"])
        self.assertNotIn(key.encode(), (common.STATE_DIR / "gemini.dpapi").read_bytes())
        payload["revision"] = json.loads(body)["revision"]
        with patch.object(common, "fetch_models", return_value=["gemini-alias", "vendor-exact-ID"]) as fetch:
            status, body, _ = await self.request("api/models", "POST", {**payload, "key": ""})
            self.assertEqual(json.loads(body)["models"], ["gemini-alias", "vendor-exact-ID"])
            fetch.assert_called_once_with("https://relay.example.com", key, "gemini")
        with patch.object(common, "fetch_models") as fetch:
            status, _, _ = await self.request("api/models", "POST", {
                "base_url": "https://another.example.com", "key": ""})
            self.assertEqual(status, 400)
            fetch.assert_not_called()

    async def test_network_errors_never_echo_entered_key(self):
        key = "panel-fake-sensitive-token"
        with patch.object(common, "fetch_models", side_effect=RuntimeError("upstream echoed " + key)):
            status, body, _ = await self.request("api/models", "POST", {
                "base_url": "https://relay.example.com", "key": key})
        self.assertEqual(status, 400)
        self.assertNotIn(key.encode(), body)
        self.assertIn("已隐藏", json.loads(body)["error"])

    async def test_task_lifecycle_persistence_and_empty_result_failure(self):
        common.save_key("panel-fixture-credential")
        common.save_settings("https://relay.example.com", "gemini-alias")
        class Process:
            returncode = 0
            async def communicate(self, _payload):
                return json.dumps({"ok": True, "text": "review finished"}).encode(), b""
        async def create(*_args, **_kwargs):
            return Process()
        with patch.object(asyncio, "create_subprocess_exec", side_effect=create):
            status, body, _ = await self.request("api/tasks", "POST", {
                "task": "review the project", "project_dir": str(APP), "timeout_seconds": 30})
            self.assertEqual(status, 200)
            submitted = json.loads(body)
            self.assertEqual(submitted["mode"], "review")
            result = await server.get_job(submitted["job_id"], wait_seconds=1)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["result"], "review finished")
        server.JOBS.clear()
        loaded = await server.get_job(submitted["job_id"])
        self.assertEqual(loaded["result"], "review finished")
        _, body, _ = await self.request("api/jobs")
        snapshot = json.loads(body)["jobs"][0]
        self.assertEqual(snapshot["project_name"], APP.name)
        self.assertEqual(snapshot["project_dir"], str(APP))
        self.assertNotIn("_control_url", snapshot)
        self.assertNotIn("_pid", snapshot)
        self.assertNotIn("result", snapshot)
        self.assertEqual(snapshot["result_chars"], len("review finished"))
        class EmptyProcess(Process):
            async def communicate(self, _payload):
                return b'{"ok":true,"text":""}', b""
        async def create_empty(*_args, **_kwargs):
            return EmptyProcess()
        with patch.object(asyncio, "create_subprocess_exec", side_effect=create_empty):
            job = await server.submit_task("empty", str(APP))
            result = await server.get_job(job["job_id"], wait_seconds=1)
            self.assertEqual(result["status"], "failed")
            self.assertIn("未确认完成", result["error"])

    async def test_running_job_remains_alive_while_panel_is_polled(self):
        common.save_key("panel-fixture-credential")
        common.save_settings("https://relay.example.com", "gemini-alias")
        finished = asyncio.Event()
        class Process:
            returncode = 0
            async def communicate(self, _payload):
                await finished.wait()
                return b'{"ok":true,"text":"finished"}', b""
        async def create(*_args, **_kwargs):
            return Process()
        with patch.object(asyncio, "create_subprocess_exec", side_effect=create):
            job = await server.submit_task("poll live worker", str(APP))
            _, body, _ = await self.request("api/jobs")
            self.assertEqual(json.loads(body)["jobs"][0]["status"], "running")
            self.assertEqual((await server.get_job(job["job_id"]))["status"], "running")
            finished.set()
            self.assertEqual((await server.get_job(job["job_id"], wait_seconds=1))["status"], "completed")

    async def test_task_validation_and_interrupted_history(self):
        for payload in ({"mode": "invalid"}, {"timeout_seconds": True}, {"timeout_seconds": "600"}):
            self.assertEqual((await self.request("api/tasks", "POST", payload))[0], 400)
        for invalid in ("../anything", "x" * 16):
            with self.assertRaises(common.RelayError):
                await server.get_job(invalid)
        job = {"job_id": "0" * 16, "status": "running", "project_dir": str(APP),
               "_pid": 0, "_control_url": self.url, "started_at": progress.now()}
        progress.save_job(job)
        _, body, _ = await self.request("api/jobs")
        self.assertEqual(json.loads(body)["jobs"][0]["status"], "interrupted")
        self.assertEqual((await server.cancel_job(job["job_id"]))["status"], "interrupted")

    async def test_stale_configuration_cannot_replace_profile_or_key(self):
        common.save_settings("https://relay.example.com", "first", "claude")
        common.save_key("original-fixture", "claude")
        revision = common.profile_revision("claude")
        common.save_settings("https://relay.example.com", "external-model", "claude")
        payload = {"backend": "claude", "base_url": "https://relay.example.com", "model": "first",
                   "effort": "high", "key": "replacement-fixture", "revision": revision}
        status, body, _ = await self.request("api/config", "POST", payload)
        self.assertEqual(status, 409)
        self.assertIn("配置已更新", json.loads(body)["error"])
        self.assertEqual(common.load_settings("claude")["model"], "external-model")
        self.assertEqual(common.load_key("claude"), "original-fixture")
        self.assertNotIn(b"replacement-fixture", body)
        payload.pop("revision")
        self.assertEqual((await self.request("api/config", "POST", payload))[0], 409)

    async def test_credential_rotation_changes_revision_and_rejects_old_catalog(self):
        common.save_settings("https://relay.example.com", "model")
        common.save_key("first-fixture")
        revision = common.profile_revision("gemini")
        self.assertEqual(common.profile_revision("gemini"), revision)
        common.save_key("second-fixture")
        self.assertNotEqual(common.profile_revision("gemini"), revision)
        with patch.object(common, "fetch_models") as fetch:
            status, _, _ = await self.request("api/models", "POST", {
                "base_url": "https://relay.example.com", "revision": revision})
            self.assertEqual(status, 409)
            fetch.assert_not_called()

    async def test_full_result_pagination_and_invalid_panel_parameters(self):
        full = "汉字🙂abc\n" * 15000
        job = {"job_id": "a" * 16, "status": "completed", "result": full, "project_dir": str(APP)}
        progress.save_job(job)
        _, body, _ = await self.request("api/jobs")
        summary = json.loads(body)["jobs"][0]
        self.assertEqual(summary["result_chars"], len(full))
        self.assertNotIn("result", summary)
        self.assertLess(len(body), 3000)
        offset, pieces = 0, []
        while offset is not None:
            status, body, _ = await self.request("api/job", "POST", {"job_id": job["job_id"], "offset": offset})
            self.assertEqual(status, 200)
            page = json.loads(body)
            self.assertEqual(page["offset"], offset)
            self.assertEqual(page["total_chars"], len(full))
            pieces.append(page["result"])
            offset = page["next_offset"]
        self.assertEqual("".join(pieces), full)
        for invalid in ({"offset": True}, {"offset": -1}, {"offset": "0"}, {"max_chars": None}, {"max_chars": 50001}):
            self.assertEqual((await self.request("api/job", "POST", {"job_id": job["job_id"], **invalid}))[0], 400)

    async def test_short_title_and_full_redacted_description_survive_restart_without_bloating_polling(self):
        common.save_key("description-fixture-secret")
        common.save_settings("https://relay.example.com", "gemini-alias")
        description = "完整要求🙂\n" * 16000 + "尾部验证 description-fixture-secret"
        title = "审查前端交互与可访问性"
        release = asyncio.Event()

        async def finish(job, *_args):
            await release.wait()
            job.update(status="completed", result="OK")
            progress.save_job(job)

        with patch.object(server, "execute", new=finish):
            status, body, _ = await self.request("api/tasks", "POST", {
                "task": description, "title": title, "project_dir": str(APP)})
            self.assertEqual(status, 200)
            submitted = json.loads(body)
            self.assertEqual(submitted["title"], title)
            self.assertNotIn("task_description", submitted)
            _, body, _ = await self.request("api/jobs")
            self.assertNotIn("task_description", json.loads(body)["jobs"][0])
            self.assertLess(len(body), 3000)
            _, body, _ = await self.request("api/job", "POST", {"job_id": submitted["job_id"]})
            detail = json.loads(body)
            self.assertEqual(detail["status"], "running")
            self.assertEqual(detail["task_description"], common.redact(description, "description-fixture-secret"))
            self.assertTrue(detail["task_description_complete"])
            self.assertNotIn(b"description-fixture-secret", body)
            release.set()
            await server.get_job(submitted["job_id"], wait_seconds=1)
        server.JOBS.clear()
        detail = await server.get_job(submitted["job_id"])
        self.assertEqual(detail["title"], title)
        self.assertIn("尾部验证", detail["task_description"])
        _, body, _ = await self.request("api/job", "POST", {
            "job_id": submitted["job_id"], "include_description": False})
        self.assertNotIn("task_description", json.loads(body))
        self.assertLess(len(body), 3000)

    async def test_legacy_title_is_preserved_as_incomplete_description(self):
        legacy = "审查 UI 交互。" + "历史截断内容" * 30
        job = {"job_id": "d" * 16, "title": legacy, "status": "completed", "project_dir": str(APP)}
        progress.save_job(job)
        detail = await server.get_job(job["job_id"])
        self.assertEqual(detail["title"], "审查 UI 交互")
        self.assertEqual(detail["task_description"], legacy)
        self.assertFalse(detail["task_description_complete"])
        self.assertEqual(progress.load_job(job["job_id"])["title"], legacy)

    async def test_title_fallback_does_not_cut_mid_sentence_and_validates_explicit_titles(self):
        long_task = "A long uninterrupted task description " * 300
        self.assertEqual(progress.task_title(long_task, "edit", str(APP)), "编辑任务 · " + APP.name)
        for value in ("x" * 81, True, []):
            with self.assertRaisesRegex(common.RelayError, 'title'):
                await server.submit_task('review', str(APP), title=value)


if __name__ == "__main__":
    unittest.main()
