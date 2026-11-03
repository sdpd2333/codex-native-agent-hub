"""Windows readers can briefly prevent replacing a task or management record."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-atomic-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "record.json"
        self.path.write_bytes(b"original")

    @unittest.skipUnless(os.name == "nt", "Windows file sharing behavior")
    def test_real_open_reader_does_not_fail_record_update(self):
        real_replace = os.replace
        reader = self.path.open("rb")
        conflicts = []
        def replace(source, destination):
            try:
                return real_replace(source, destination)
            except PermissionError as exc:
                conflicts.append(exc.winerror)
                reader.close()  # The list poll finishes reading; the writer must recover.
                raise
        try:
            with patch.object(common.os, "replace", side_effect=replace):
                common._atomic_write(self.path, b"updated")
        finally:
            reader.close()
        self.assertEqual(conflicts, [5])
        self.assertEqual(self.path.read_bytes(), b"updated")
        self.assertFalse(list(self.path.parent.glob(".tmp-*")))

    def test_persistent_sharing_denial_is_bounded_and_keeps_original(self):
        error = PermissionError("fixture sharing violation")
        error.winerror = 32
        with patch.object(common.os, "replace", side_effect=error) as replace, patch.object(common.time, "sleep"):
            with self.assertRaises(PermissionError):
                common._atomic_write(self.path, b"new")
        self.assertEqual(replace.call_count, 6)
        self.assertEqual(self.path.read_bytes(), b"original")
        self.assertFalse(list(self.path.parent.glob(".tmp-*")))

    def test_other_permission_errors_are_not_retried(self):
        with patch.object(common.os, "replace", side_effect=PermissionError("fixture denial")) as replace:
            with self.assertRaises(PermissionError):
                common._atomic_write(self.path, b"new")
        self.assertEqual(replace.call_count, 1)
        self.assertEqual(self.path.read_bytes(), b"original")
        self.assertFalse(list(self.path.parent.glob(".tmp-*")))


if __name__ == "__main__":
    unittest.main()
