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
        full = "\n汉字🙂汉字Ω丙丁\r" * 100000  # Every page splits a CRLF pair.
        self.assertEqual(len(full), 1_000_000)
        self.assertGreater(len(full.encode()), 2 * 1024 * 1024)
        progress.save_job(self.job | {"result": full})
        metadata = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertNotIn("result", metadata)
        self.assertLess(self.record.stat().st_size, 2048)
        self.assertEqual(metadata["_result_chars"], len(full))
        with patch.object(progress, "result_page", side_effect=AssertionError("History read result file")):
            listed = history.query()["jobs"][0]
        self.assertEqual(listed["result_chars"], len(full))
        self.assertNotIn("_result_file", listed)
        path = self.record.parent / metadata["_result_file"]
        opener = Path.open
        decoded = 0
        def count_reads(file, *args, **kwargs):
            stream = opener(file, *args, **kwargs)
            if file == path:
                read = stream.read
                def count(size=-1):
                    nonlocal decoded
                    text = read(size)
                    decoded += len(text)
                    return text
                stream.read = count
            return stream
        pieces, offset, cursor = [], 0, None
        with patch.object(Path, "open", new=count_reads):
            while offset is not None:
                page = await server.get_job(self.job["job_id"], offset=offset, max_chars=10000,
                                            include_description=False, cursor=cursor)
                self.assertEqual(page["offset"], offset)
                self.assertEqual(page["total_chars"], len(full))
                self.assertNotIn("_result_file", page)
                pieces.append(page["result"])
                offset, cursor = page["next_offset"], page.get("next_cursor")
        self.assertEqual(len(pieces), 100)
        self.assertEqual(decoded, len(full))
        self.assertEqual("".join(pieces), full)
        beyond = await server.get_job(self.job["job_id"], offset=len(full) + 1)
        self.assertEqual(beyond["result"], "")
        self.assertIsNone(beyond["next_offset"])

    async def test_cursor_binds_job_result_and_offset_and_survives_a_cold_cache(self):
        full = "a\r汉🙂\n" * 110
        other = self.job | {"job_id": "0" * 16}
        for job in (self.job, other):
            progress.save_job(job | {"result": full})
        first = await server.get_job(self.job["job_id"], max_chars=101)
        offset, cursor = first["next_offset"], first["next_cursor"]
        progress._page_positions.clear()  # Evicted checkpoint or service restart.
        page = await server.get_job(self.job["job_id"], offset=offset, max_chars=103, cursor=cursor)
        self.assertEqual(page["result"], full[offset:offset + 103])
        for job_id, start, token in ((other["job_id"], offset, cursor), (self.job["job_id"], offset + 1, cursor),
                                     (self.job["job_id"], offset, "bad-cursor")):
            with self.subTest(start=start, job_id=job_id), self.assertRaisesRegex(common.RelayError, "游标"):
                await server.get_job(job_id, offset=start, cursor=token)
        progress.save_job(self.job | {"result": "x" * len(full)})
        with self.assertRaisesRegex(common.RelayError, "游标"):
            await server.get_job(self.job["job_id"], offset=offset, cursor=cursor)

    async def test_truncated_file_cannot_satisfy_an_issued_cursor(self):
        progress.save_job(self.job | {"result": "测🙂\r\n" * 250})
        first = await server.get_job(self.job["job_id"], max_chars=100)
        path = self.record.parent / json.loads(self.record.read_bytes())["_result_file"]
        path.write_bytes(first["result"].encode())
        with self.assertRaisesRegex(common.RelayError, "不完整"):
            await server.get_job(self.job["job_id"], offset=first["next_offset"], cursor=first["next_cursor"])

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
