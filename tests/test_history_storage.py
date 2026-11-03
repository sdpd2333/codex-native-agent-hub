from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common
import events
import history
import progress
import storage


class HistoryStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-history-test-")
        self.root = Path(self.temp.name) / "state"
        self.root.mkdir()
        self.state = patch.object(common, "STATE_DIR", self.root)
        self.state.start()
        progress._trackers.clear()
        history._metadata.clear()
        storage._previews.clear()
        self.old = datetime.now(timezone.utc) - timedelta(days=60)

    def tearDown(self):
        self.state.stop()
        self.temp.cleanup()

    def job(self, number=1, **values):
        job = {"job_id": f"{number:016x}", "conversation_id": "chat-a", "backend": "claude", "model": "test-model",
               "title": f"Task {number}", "task_description": "full task description", "result": "full result",
               "status": "completed", "started_at": self.old.isoformat(), "finished_at": self.old.isoformat()}
        job.update(values)
        progress.save_job(job)
        return job

    def log(self, job, name="session/data.txt", data=b"log payload", old=True):
        path = progress.job_dir(job["job_id"]) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if old:
            os.utime(path, (self.old.timestamp(), self.old.timestamp()))
        return path

    def link_dir(self, source, target):
        if os.name == "nt":
            # A junction needs no symlink privilege; remove it with rmdir in test teardown.
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(source), str(target)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            if result.returncode:
                self.skipTest("Junction creation unavailable")
            self.addCleanup(lambda: source.rmdir() if source.exists() else None)
        else:
            source.symlink_to(target, target_is_directory=True)

    def test_current_filter_precedes_pagination_and_results_load_on_demand(self):
        expected = self.job(title="完整短标题。包含标点", result="large result" * 5000)
        for number in range(2, 74):
            self.job(number, conversation_id="chat-b", started_at=progress.now())
        current = history.query(conversation="chat-a", view="current")
        self.assertEqual(current["total"], 1)
        self.assertEqual(current["jobs"][0]["job_id"], expected["job_id"])
        self.assertEqual(current["jobs"][0]["title"], expected["title"])
        self.assertEqual(current["jobs"][0]["result_chars"], len(expected["result"]))
        self.assertNotIn("result", current["jobs"][0])
        self.assertNotIn("task_description", current["jobs"][0])
        self.assertEqual(history.query(view="all")["next_offset"], 50)
        self.assertEqual(history.query(view="all", offset=50)["total"], 73)
        self.assertEqual(len(history.query(view="all", offset=500)["jobs"]), 23)
        self.assertEqual(history.query(conversation="chat-c", view="current")["total"], 0)

    def test_running_first_counts_legacy_and_attention_search(self):
        self.job()
        live = self.job(2, status="running", conversation_id="chat-b", _pid=os.getpid())
        self.job(3, status="failed", conversation_id="", title="Legacy UI")
        result = history.query(conversation="chat-a", view="all", limit=1)
        self.assertEqual(result["jobs"][0]["job_id"], live["job_id"])
        self.assertEqual((result["running_count"], result["other_running_count"]), (1, 1))
        self.assertEqual(history.query(status="attention", search="legacy")["total"], 1)
        self.assertEqual(history.query(view="current")["total"], 0)

    def test_unchanged_history_does_not_reread_full_results_but_worker_updates_invalidate_cache(self):
        job = self.job(result="result" * 10000)
        with patch.object(progress, "load_job", wraps=progress.load_job) as load:
            history.query()
            history.query()
            self.assertEqual(load.call_count, 1)
            job.update(result="updated result", status="failed")
            progress.save_job(job)
            changed = history.query()["jobs"][0]
            self.assertEqual(load.call_count, 2)
            self.assertEqual(changed["result_chars"], len("updated result"))
            self.assertEqual(changed["status"], "failed")
        cached = next(iter(history._metadata.values()))[1]
        self.assertNotIn("task_description", cached)
        self.assertNotIn("result", cached)

    def test_archive_restore_survives_worker_save_and_restart(self):
        job = self.job()
        history.archive(job["job_id"], True)
        progress.save_job(job)  # An old worker does not know about management fields.
        self.assertEqual(history.query()["total"], 0)
        self.assertTrue(history.query(view="archived")["jobs"][0]["archived"])
        self.assertEqual(progress.load_job(job["job_id"])["result"], job["result"])
        history.archive(job["job_id"], False)
        self.assertEqual(history.query(conversation="chat-a", view="current")["total"], 1)
        self.assertEqual(history.query(view="archived")["total"], 0)
        live = self.job(2, status="running", _pid=os.getpid())
        with self.assertRaises(common.RelayError):
            history.archive(live["job_id"], True)

    def test_invalid_history_queries_rejected(self):
        for value in ("../chat", "x" * 129, [], True):
            with self.subTest(value=value), self.assertRaises(common.RelayError):
                history.conversation_id(value)
        for params in ({"view": "invalid"}, {"status": "invalid"}, {"offset": True}, {"limit": 101},
                       {"search": []}, {"offset": -1}, {"search": "x" * 301}):
            with self.subTest(params=params), self.assertRaises(common.RelayError):
                history.query(**params)

    def test_storage_policy_revisions_and_validation(self):
        original = storage.policy()
        self.assertEqual(original["retention_days"], 30)
        saved = storage.save_policy(90, original["revision"])
        self.assertEqual(storage.policy(), saved)
        with self.assertRaises(common.ConfigurationConflict):
            storage.save_policy(60, original["revision"])
        for invalid in (True, 0, 3651, "30", None, 1.5):
            with self.assertRaises(common.RelayError):
                storage.preview(invalid)

    def test_cleanup_preserves_records_results_credentials_worktrees_and_counters(self):
        job = self.job()
        directory = progress.job_dir(job["job_id"])
        logs = [self.log(job, name) for name in (
            "runtime/brain/session.txt", "session/transcript.txt", "claude/projects/data.txt", "dsh/session.txt", "dsh-profile.yml")]
        writer = events.EventWriter(directory)
        writer.model("first")
        writer.tool("read", "read")
        event_path = writer.path
        os.utime(event_path, (self.old.timestamp(), self.old.timestamp()))
        before = progress.snapshot(job)["progress"]
        self.assertEqual((before["model_calls"], before["tool_calls"]), (1, 1))
        history.archive(job["job_id"], True)
        keep = [self.root / "settings.json", self.root / "claude.dpapi", self.root / "worktrees" / "edit" / "code.py",
                directory / "unknown.txt"]
        for path in keep:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"must survive")
        original_record = (directory / "job.json").read_bytes()
        overview = storage.overview()
        self.assertGreater(overview["bytes"]["logs"], 0)
        self.assertEqual(overview["bytes"]["worktrees"], len(b"must survive"))
        plan = storage.preview(30)
        self.assertEqual(plan["job_count"], 1)
        self.assertTrue(all(path.exists() for path in logs + [event_path]))
        cleaned = storage.cleanup(plan["token"])
        self.assertEqual(cleaned["deleted_files"], 6)
        self.assertEqual(cleaned["freed_bytes"], plan["bytes"])
        self.assertEqual(cleaned["errors"], 0)
        self.assertTrue(all(path.read_bytes() == b"must survive" for path in keep))
        self.assertEqual((directory / "job.json").read_bytes(), original_record)
        after = progress.snapshot(job)
        self.assertTrue(after["archived"])
        self.assertEqual(after["progress"], before)
        self.assertTrue(after["logs_cleaned_at"])
        self.assertEqual(storage.preview(30)["file_count"], 0)
        with self.assertRaises(common.RelayError):
            storage.cleanup(plan["token"])

    def test_preview_excludes_live_recent_and_invalid_records_and_recent_files(self):
        values = [{"status": "running", "_pid": os.getpid()}, {"status": "running", "_pid": 0},
                  {"finished_at": progress.now()}, {"finished_at": "not-a-date"}, {"finished_at": None},
                  {"finished_at": "2026-01-01T00:00:00"}]
        for index, value in enumerate(values, 1):
            self.log(self.job(index, **value))
        self.log(self.job(7), old=False)
        self.assertEqual(storage.preview(30)["file_count"], 0)

    def test_changed_files_and_new_files_are_preserved(self):
        job = self.job()
        changed = self.log(job, "session/changed.txt")
        unchanged = self.log(job, "session/unchanged.txt")
        plan = storage.preview(30)
        changed.write_bytes(b"modified after preview")
        added = self.log(job, "session/added.txt")
        result = storage.cleanup(plan["token"])
        self.assertEqual(result["deleted_files"], 1)
        self.assertEqual(result["skipped_files"], 1)
        self.assertTrue(changed.exists() and added.exists())
        self.assertFalse(unchanged.exists())

    def test_expired_preview_and_job_that_resumed_are_not_cleaned(self):
        job = self.job()
        path = self.log(job)
        plan = storage.preview(30)
        with patch.object(storage.time, "monotonic", return_value=storage._previews[plan["token"]]["expires"] + 1):
            with self.assertRaises(common.RelayError):
                storage.cleanup(plan["token"])
        plan = storage.preview(30)
        job["status"] = "running"
        progress.save_job(job)
        result = storage.cleanup(plan["token"])
        self.assertEqual(result["deleted_files"], 0)
        self.assertEqual(result["skipped_files"], 1)
        self.assertTrue(path.exists())

    def test_links_and_junctions_are_neither_counted_nor_cleaned(self):
        job = self.job()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        victim = outside / "keep.txt"
        victim.write_bytes(b"outside data")
        self.link_dir(progress.job_dir(job["job_id"]) / "session", outside)
        overview = storage.overview()
        self.assertTrue(overview["partial"])
        self.assertEqual(overview["bytes"]["logs"], 0)
        self.assertEqual(storage.preview(30)["file_count"], 0)
        self.assertTrue(victim.exists())

    def test_directory_replaced_by_junction_after_preview_is_preserved(self):
        job = self.job()
        log = self.log(job)
        plan = storage.preview(30)
        log.unlink()
        log.parent.rmdir()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        victim = outside / "data.txt"
        victim.write_bytes(b"outside data")
        self.link_dir(log.parent, outside)
        result = storage.cleanup(plan["token"])
        self.assertEqual(result["deleted_files"], 0)
        self.assertEqual(result["skipped_files"], 1)
        self.assertTrue(victim.exists())

    def test_permission_failure_is_partial_and_archive_metadata_survives(self):
        job = self.job()
        path = self.log(job)
        history.archive(job["job_id"], True)
        plan = storage.preview(30)
        original = Path.unlink
        def unlink(file, *args, **kwargs):
            if file == path:
                raise PermissionError("test denial")
            return original(file, *args, **kwargs)
        with patch.object(Path, "unlink", new=unlink):
            result = storage.cleanup(plan["token"])
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["deleted_files"], 0)
        self.assertTrue(path.exists())
        self.assertTrue(progress.snapshot(job)["archived"])


if __name__ == "__main__":
    unittest.main()
