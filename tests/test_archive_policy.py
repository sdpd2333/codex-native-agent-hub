from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common
import history
import progress
import server


class ArchivePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-archive-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "state"
        self.root.mkdir()
        self.state = patch.object(common, "STATE_DIR", self.root)
        self.state.start()
        self.addCleanup(self.state.stop)
        history._metadata.clear()
        history._previews.clear()
        self.now = datetime.now(timezone.utc)

    def job(self, number=1, **values):
        job = {"job_id": f"{number:016x}", "status": "completed", "backend": "claude",
               "title": f"Task {number}", "task_description": "full description", "result": "full result",
               "finished_at": (self.now - timedelta(days=8)).isoformat(), "conversation_id": "chat-a"}
        job.update(values)
        progress.save_job(job)
        return job

    def test_defaults_revision_and_input_validation(self):
        original = history.policy()
        self.assertTrue(original["enabled"])
        self.assertEqual(original["after_days"], 7)
        saved = history.save_policy(False, 30, original["revision"])
        self.assertEqual(history.policy(), saved)
        with self.assertRaises(common.ConfigurationConflict):
            history.save_policy(True, 7, original["revision"])
        for enabled, days in [(1, 7), (None, 7), (True, True), (True, 0), (True, 3651), (True, "7"), (True, 1.5)]:
            with self.subTest(enabled=enabled, days=days), self.assertRaises(common.RelayError):
                history.save_policy(enabled, days, saved["revision"])

    def test_due_successes_archive_and_preserve_data(self):
        old = self.job()
        legacy = self.job(2, conversation_id="")
        progress.update_management(old["job_id"], activity_summary={"model_calls": 3}, logs_cleaned_at="kept")
        original = (progress.job_dir(old["job_id"]) / "job.json").read_bytes()
        self.assertEqual(history.query()["total"], 0)
        self.assertEqual(history.query(view="archived")["total"], 2)
        self.assertEqual(history.auto_archive(), 0)
        for job in (old, legacy):
            snapshot = progress.snapshot(job)
            self.assertEqual(snapshot["archive_reason"], "automatic")
            self.assertTrue(snapshot["archived_at"])
        self.assertEqual((progress.job_dir(old["job_id"]) / "job.json").read_bytes(), original)
        self.assertEqual(progress.load_management(old["job_id"])["activity_summary"], {"model_calls": 3})

    def test_age_boundary_timezone_and_exclusions(self):
        cutoff = self.now - timedelta(days=7)
        for stamp in (cutoff.isoformat(), cutoff.astimezone(timezone(timedelta(hours=8))).isoformat(),
                      cutoff.isoformat().replace("+00:00", "Z")):
            self.assertTrue(history.aged({"finished_at": stamp}, cutoff))
        self.assertFalse(history.aged({"finished_at": (cutoff + timedelta(microseconds=1)).isoformat()}, cutoff))
        self.job(finished_at=self.now.isoformat())
        for i, status in enumerate(("running", "failed", "cancelled", "interrupted", "timed_out"), 2):
            self.job(i, status=status, _pid=os.getpid())
        for i, stamp in enumerate((None, "", "invalid", "2020-01-01T00:00:00", (self.now + timedelta(days=1)).isoformat()), 7):
            self.job(i, finished_at=stamp)
        self.assertEqual(history.auto_archive(), 0)
        self.assertEqual(history.query(status="attention")["total"], 4)

    def test_disabled_and_configured_interval(self):
        self.job()
        history.save_policy(False, 7, history.policy()["revision"])
        self.assertEqual(history.auto_archive(), 0)
        history.save_policy(True, 10, history.policy()["revision"])
        self.assertEqual(history.auto_archive(), 0)
        history.save_policy(True, 1, history.policy()["revision"])
        self.assertEqual(history.auto_archive(), 1)

    def test_restore_exemption_survives_restart_worker_save_and_policy_change(self):
        job = self.job(conversation_id="")
        self.assertEqual(history.auto_archive(), 1)
        history.archive(job["job_id"], False)
        progress.save_job(job)
        history._metadata.clear()
        history.save_policy(True, 1, history.policy()["revision"])
        self.assertEqual(history.query()["total"], 1)
        self.assertEqual(history.preview_legacy()["job_count"], 0)
        snapshot = progress.snapshot(job)
        self.assertTrue(snapshot["auto_archive_exempt"])
        self.assertTrue(snapshot["restored_at"])
        history.archive(job["job_id"], True)
        self.assertEqual(progress.snapshot(job)["archive_reason"], "manual")

    def test_concurrent_restore_and_policy_disable_win_over_sweep(self):
        job = self.job()
        real_policy = history.policy
        calls = 0
        def policy_with_restore():
            nonlocal calls
            calls += 1
            if calls == 2:
                history.archive(job["job_id"], False)
            return real_policy()
        with patch.object(history, "policy", side_effect=policy_with_restore):
            self.assertEqual(history.auto_archive(), 0)
        progress.update_management(job["job_id"], auto_archive_exempt=False)
        calls = 0
        def policy_with_disable():
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.root / "archive-policy.json").write_text(json.dumps({"enabled": False, "after_days": 7}))
            return real_policy()
        with patch.object(history, "policy", side_effect=policy_with_disable):
            self.assertEqual(history.auto_archive(), 0)

    def test_preview_is_read_only_and_bulk_rechecks_exact_candidates(self):
        jobs = [self.job(i, conversation_id="", finished_at=self.now.isoformat()) for i in range(1, 8)]
        self.job(8)  # assigned, excluded
        self.job(9, status="failed", conversation_id="")
        plan = history.preview_legacy()
        self.assertEqual(plan["job_count"], 7)
        self.assertFalse(any(progress.load_management(job["job_id"]) for job in jobs))
        jobs[0]["status"] = "running"
        jobs[1]["conversation_id"] = "assigned-after-preview"
        jobs[2]["result"] = "changed after preview"
        for job in jobs[:3]:
            progress.save_job(job)
        history.archive(jobs[3]["job_id"], True)
        history.archive(jobs[4]["job_id"], False)
        self.job(10, conversation_id="")  # never present in the manifest
        result = history.archive_legacy(plan["token"])
        self.assertEqual(result, {"archived": 2, "skipped": 5, "errors": 0})
        self.assertEqual(progress.snapshot(jobs[6])["archive_reason"], "legacy")
        self.assertFalse(progress.load_management(f"{10:016x}"))
        with self.assertRaises(common.RelayError):
            history.archive_legacy(plan["token"])

    def test_expiry_root_change_and_token_validation(self):
        self.job(conversation_id="")
        plan = history.preview_legacy()
        with patch.object(history.time, "monotonic", return_value=history._previews[plan["token"]]["expires"]):
            with self.assertRaises(common.RelayError):
                history.archive_legacy(plan["token"])
        plan = history.preview_legacy()
        with patch.object(common, "STATE_DIR", self.root / "elsewhere"), self.assertRaises(common.RelayError):
            history.archive_legacy(plan["token"])
        for token in (None, [], "unknown"):
            with self.assertRaises(common.RelayError):
                history.archive_legacy(token)

    def test_corrupt_policy_keeps_history_available(self):
        self.job()
        (self.root / "archive-policy.json").write_text("invalid JSON")
        result = history.query()
        self.assertEqual(result["total"], 1)
        self.assertTrue(result["archive_error"])

    def test_job_directory_replaced_by_link_is_never_mutated(self):
        job = self.job(conversation_id="")
        plan = history.preview_legacy()
        source = progress.job_dir(job["job_id"])
        outside = Path(self.temp.name) / "outside"
        source.rename(outside)
        if os.name == "nt":
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(source), str(outside)], capture_output=True)
            if result.returncode:
                self.skipTest("Junction creation unavailable")
            self.addCleanup(source.rmdir)
        else:
            source.symlink_to(outside, target_is_directory=True)
        self.assertEqual(history.preview_legacy()["job_count"], 0)
        self.assertEqual(history.auto_archive(), 0)
        self.assertEqual(history.archive_legacy(plan["token"])["errors"], 1)
        with self.assertRaises(common.RelayError):
            history.archive(job["job_id"], True)
        self.assertFalse((outside / "management.json").exists())


class ArchiveLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_runs_maintenance_and_joins_on_shutdown(self):
        import asyncio
        with patch.object(history, "auto_archive", return_value=0) as sweep:
            async with server.lifespan(server.mcp):
                for _ in range(100):
                    if sweep.called:
                        break
                    await asyncio.sleep(0.001)
                self.assertEqual(sweep.call_count, 1)
            self.assertEqual(sweep.call_count, 1)


if __name__ == "__main__":
    unittest.main()
