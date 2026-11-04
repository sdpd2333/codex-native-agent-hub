"""Connection changes must never mix an address with another provider's credential."""
import asyncio
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import common
import server


class ConnectionIntegrityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-connection-")
        self.addCleanup(self.temp.cleanup)
        self.enterContext(patch.object(common, "STATE_DIR", Path(self.temp.name)))
        self.enterContext(patch.dict(os.environ, {"AGENT_HUB_" + b.upper() + "_API_KEY": ""
                                                 for b in common.PROVIDERS}))
        common.save_settings("https://old.example.com", "claude-fixture", "claude")
        common.save_key("old-fixture-key", "claude")

    def payload(self, backend="claude", **values):
        return {"backend": backend, "base_url": "https://new.example.com", "model": backend + "-fixture",
                "key": "new-fixture-key", "revision": common.profile_revision(backend), **values}

    async def test_environment_override_rejects_both_destination_and_credential_changes(self):
        before = [(common.STATE_DIR / name).read_bytes() for name in ("settings.json", "claude.dpapi")]
        with patch.dict(os.environ, {"AGENT_HUB_CLAUDE_API_KEY": "old-env-fixture"}):
            for base in ("https://old.example.com", "https://new.example.com"):
                with self.subTest(base=base), self.assertRaisesRegex(common.RelayError, "环境变量") as raised:
                    await server.panel_dispatch("POST", "config", self.payload(base_url=base))
                self.assertNotIn("old-env-fixture", str(raised.exception))
                self.assertNotIn("new-fixture-key", str(raised.exception))
        self.assertEqual(before, [(common.STATE_DIR / name).read_bytes()
                                  for name in ("settings.json", "claude.dpapi")])

    async def test_matching_environment_key_can_be_explicitly_saved(self):
        with patch.dict(os.environ, {"AGENT_HUB_CLAUDE_API_KEY": "new-fixture-key"}):
            result = await server.panel_dispatch("POST", "config", self.payload())
            self.assertTrue(result["ok"])
            self.assertEqual(common.load_settings("claude")["base_url"], "https://new.example.com")
            self.assertEqual(common.load_key("claude"), "new-fixture-key")
        self.assertEqual(common.load_key("claude"), "new-fixture-key")

    async def test_profile_write_failure_restores_key_and_all_profiles(self):
        common.save_settings("https://other.example.com", "gemini-fixture", "gemini")
        before = [(common.STATE_DIR / name).read_bytes() for name in ("settings.json", "claude.dpapi")]
        write = common._atomic_write

        def fail_settings(path, data):
            if path.name == "settings.json":
                raise PermissionError("fixture settings write failure")
            write(path, data)

        with patch.object(common, "_atomic_write", side_effect=fail_settings):
            with self.assertRaises(common.RelayError):
                await server.panel_dispatch("POST", "config", self.payload())
        self.assertEqual(before, [(common.STATE_DIR / name).read_bytes()
                                  for name in ("settings.json", "claude.dpapi")])
        self.assertEqual(common.load_key("claude"), "old-fixture-key")

    async def test_failed_first_save_removes_the_new_credential(self):
        before = (common.STATE_DIR / "settings.json").read_bytes()
        with patch.object(common, "save_settings", side_effect=OSError("fixture write failure")):
            with self.assertRaises(common.RelayError):
                await server.panel_dispatch("POST", "config", self.payload("deepseek"))
        self.assertFalse(common.has_key("deepseek"))
        self.assertEqual((common.STATE_DIR / "settings.json").read_bytes(), before)

    async def test_network_reads_keep_one_snapshot_during_a_concurrent_save(self):
        for endpoint in ("list_models", "test_connection"):
            with self.subTest(endpoint=endpoint):
                common.save_settings("https://old.example.com", "claude-fixture", "claude")
                common.save_key("old-fixture-key", "claude")
                read, attempted, written = threading.Event(), threading.Event(), threading.Event()
                errors = []
                original = common.load_settings

                def writer():
                    try:
                        if not read.wait(5):
                            raise RuntimeError("Reader did not start")
                        attempted.set()
                        with common.configuration_lock():
                            common.save_key("new-fixture-key", "claude")
                            common.save_settings("https://new.example.com", "claude-new", "claude")
                    except Exception as exc:
                        errors.append(exc)
                    finally:
                        written.set()

                def halfway(backend):
                    value = original(backend)
                    read.set()
                    self.assertTrue(attempted.wait(5))
                    # Give the competing writer a chance while the reader holds its snapshot lock.
                    written.wait(0.15)
                    return value

                def network(*args):
                    self.assertTrue(written.wait(5))
                    self.assertEqual(args[:2], ("https://old.example.com", "old-fixture-key"))
                    return ["claude-fixture"] if endpoint == "list_models" else "OK"

                worker = threading.Thread(target=writer)
                worker.start()
                try:
                    target = "fetch_models" if endpoint == "list_models" else "test_native_model"
                    with patch.object(common, "load_settings", side_effect=halfway), \
                         patch.object(common, target, side_effect=network):
                        await getattr(server, endpoint)("claude")
                finally:
                    read.set()
                    await asyncio.to_thread(worker.join, 5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
