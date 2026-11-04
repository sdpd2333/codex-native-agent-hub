"""Large results stay readable across paging, restarts, failed writes and log cleanup."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import common
import history
import progress
import server
import storage
import app_settings


class ResultStorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-results-")
        self.addCleanup(self.temp.cleanup)
        self.enterContext(patch.object(common, "STATE_DIR", Path(self.temp.name)))
        server.JOBS.clear()
        progress._trackers.clear()
        history._metadata.clear()
        history.save_policy(False, 7, history.policy()["revision"])
        self.job = {"job_id": "0123456789abcdef", "backend": "claude", "status": "completed",
                    "title": "Large result fixture", "task_description": "Keep the complete result",
                    "finished_at": (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()}
        self.record = progress.job_dir(self.job["job_id"]) / "job.json"

    async def test_large_unicode_result_survives_restart_and_complete_paging(self):
        full = "汉字🙂abc\r\n" * 160000
        self.assertGreater(len(full.encode()), 2 * 1024 * 1024)
        progress.save_job(self.job | {"result": full})
        metadata = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertNotIn("result", metadata)
        self.assertLess(self.record.stat().st_size, 2048)
        self.assertEqual(metadata["_result_chars"], len(full))
        with patch.object(progress, "read_result", side_effect=AssertionError("History read full result")):
            listed = history.query()["jobs"][0]
        self.assertEqual(listed["result_chars"], len(full))
        self.assertNotIn("_result_file", listed)
        pieces, offset = [], 0
        while offset is not None:
            page = await server.get_job(self.job["job_id"], offset=offset, max_chars=50000,
                                        include_description=False)
            self.assertEqual(page["offset"], offset)
            self.assertEqual(page["total_chars"], len(full))
            self.assertNotIn("_result_file", page)
            pieces.append(page["result"])
            offset = page["next_offset"]
        self.assertEqual("".join(pieces), full)
        beyond = await server.get_job(self.job["job_id"], offset=len(full) + 1)
        self.assertEqual(beyond["result"], "")
        self.assertIsNone(beyond["next_offset"])

    async def test_budget_and_timeout_snapshots_survive_reload_and_history(self):
        limits = app_settings.task_values() | {"max_model_calls": 7, "max_tool_calls": 11}
        progress.save_job(self.job | {"runtime_settings": limits, "timeout_seconds": 123})
        loaded = progress.load_job(self.job["job_id"])
        self.assertEqual(loaded["runtime_settings"], limits)
        self.assertEqual(loaded["timeout_seconds"], 123)
        for read in (history.query()["jobs"][0], await server.get_job(self.job["job_id"])):
            self.assertEqual(read["progress"]["budget_model_calls"], 7)
            self.assertEqual(read["progress"]["budget_tool_calls"], 11)
            self.assertEqual(read["timeout_seconds"], 123)
        progress.save_job(self.job)
        self.assertEqual(history.query()["jobs"][0]["progress"]["budget_model_calls"], app_settings.DEFAULTS["max_model_calls"])

    async def test_legacy_inline_record_over_two_megabytes_remains_readable(self):
        full = "legacy-汉字🙂\n" * 180000
        original = json.dumps(self.job | {"result": full}, ensure_ascii=False).encode()
        self.assertGreater(len(original), 2 * 1024 * 1024)
        common._atomic_write(self.record, original)
        self.assertEqual(history.query()["total"], 1)
        self.assertEqual(progress.load_job(self.job["job_id"])["result"], full)
        page = await server.get_job(self.job["job_id"], offset=len(full) - 100)
        self.assertEqual(page["result"], full[-100:])
        self.assertEqual(self.record.read_bytes(), original)

    def test_failed_result_or_metadata_write_keeps_the_previous_result(self):
        progress.save_job(self.job | {"result": "original result"})
        original = self.record.read_bytes()
        write = common._atomic_write
        for fail_on in ("result", "metadata"):
            def fail(path, data):
                if (path.name == "job.json") == (fail_on == "metadata"):
                    raise PermissionError("fixture write failure")
                write(path, data)
            with self.subTest(fail_on=fail_on), patch.object(common, "_atomic_write", side_effect=fail):
                with self.assertRaises(PermissionError):
                    progress.save_job(self.job | {"result": "replacement result"})
            self.assertEqual(self.record.read_bytes(), original)
            self.assertEqual(progress.load_job(self.job["job_id"])["result"], "original result")

    async def test_log_cleanup_and_archive_restore_preserve_external_results(self):
        full = "retained-result\n" * 150000
        progress.save_job(self.job | {"result": full})
        original = self.record.read_bytes()
        metadata = json.loads(original)
        result_file = self.record.parent / metadata["_result_file"]
        log = self.record.parent / "session" / "data.txt"
        log.parent.mkdir()
        log.write_text("disposable log", encoding="utf-8")
        old = datetime.fromisoformat(self.job["finished_at"]).timestamp()
        os.utime(log, (old, old))
        history.archive(self.job["job_id"], True)
        plan = storage.preview(30)
        self.assertEqual(plan["file_count"], 1)
        self.assertEqual(storage.cleanup(plan["token"])["deleted_files"], 1)
        self.assertEqual(self.record.read_bytes(), original)
        self.assertEqual(result_file.read_bytes(), full.encode())
        history.archive(self.job["job_id"], False)
        history._metadata.clear()
        self.assertEqual(history.query()["jobs"][0]["result_chars"], len(full))
        self.assertEqual((await server.get_job(self.job["job_id"], offset=len(full) - 100))["result"], full[-100:])

    async def test_missing_result_is_reported_without_hiding_the_job(self):
        self.job["workspace"] = {"path": str(Path(self.temp.name) / "workspace"), "strategy": "direct"}
        progress.save_job(self.job | {"result": "result fixture"})
        metadata = json.loads(self.record.read_bytes())
        (self.record.parent / metadata["_result_file"]).unlink()
        self.assertEqual(history.query()["total"], 1)
        self.assertEqual((await server.cancel_job(self.job["job_id"]))["status"], "completed")
        with patch.object(server.workspaces, "changes", return_value={"diff": "fixture changes"}) as changes:
            self.assertEqual(await server.get_changes(self.job["job_id"]), {"diff": "fixture changes"})
            changes.assert_called_once_with(self.job["workspace"])
        with self.assertRaisesRegex(common.RelayError, "结果文件无法读取"):
            await server.get_job(self.job["job_id"])

    async def test_result_reference_cannot_escape_the_job_directory(self):
        progress.save_job(self.job | {"result": "fixture"})
        metadata = json.loads(self.record.read_bytes())
        metadata["_result_file"] = "../outside.txt"
        common._atomic_write(self.record, json.dumps(metadata).encode())
        with self.assertRaisesRegex(common.RelayError, "结果文件引用无效"):
            await server.get_job(self.job["job_id"])


if __name__ == "__main__":
    unittest.main()
