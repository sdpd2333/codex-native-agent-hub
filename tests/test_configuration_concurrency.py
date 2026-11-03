"""Use separate interpreters to cover the lock shared by MCP and standalone panels."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import common


class ConfigurationConcurrencyTests(unittest.TestCase):
    def test_two_processes_cannot_both_save_the_same_profile_revision(self):
        with tempfile.TemporaryDirectory(prefix="hub-config-lock-") as directory, patch.object(common, "STATE_DIR", Path(directory)):
            common.save_settings("https://relay.example.com", "initial", "claude")
            revision = common.profile_revision("claude")
            source = """
import common, sys
print('ready', flush=True)
sys.stdin.readline()
try:
    with common.configuration_lock():
        common.require_revision('claude', sys.argv[1])
        common.save_settings('https://relay.example.com', sys.argv[2], 'claude')
    print('saved', flush=True)
except common.ConfigurationConflict:
    print('conflict', flush=True)
"""
            environment = dict(os.environ, AGENT_HUB_STATE_DIR=directory, PYTHONDONTWRITEBYTECODE="1")
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            processes = [subprocess.Popen([sys.executable, "-B", "-c", source, revision, model], cwd=APP,
                         env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, **options) for model in ("first", "second")]
            try:
                for process in processes:
                    self.assertEqual(process.stdout.readline().strip(), "ready")
                for process in processes:
                    process.stdin.write("go\n")
                    process.stdin.flush()
                outcomes = []
                for process in processes:
                    output, error = process.communicate(timeout=15)
                    self.assertEqual(process.returncode, 0, error)
                    outcomes.append(output.strip())
                self.assertCountEqual(outcomes, ["saved", "conflict"])
                self.assertIn(common.load_settings("claude")["model"], ("first", "second"))
                self.assertNotEqual(common.profile_revision("claude"), revision)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()

    def test_updates_to_other_profiles_do_not_invalidate_a_draft(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(common, "STATE_DIR", Path(directory)):
            common.save_settings("https://relay.example.com", "claude-fixture", "claude")
            revision = common.profile_revision("claude")
            common.save_settings("https://relay.example.com", "gemini-fixture", "gemini")
            self.assertEqual(common.profile_revision("claude"), revision)


if __name__ == "__main__":
    unittest.main()
