"""Shared path guard contracts independent of project initialization."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
TEST_HOME = Path(__file__).resolve().parent
SOURCE = TEST_HOME.parent / "skills/project-directory-organizer/scripts/path_safety.py"


class PathSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("path_safety_under_test", SOURCE)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def setUp(self):
        runs = Path(tempfile.gettempdir()) / "project-directory-organizer-tests"
        runs.mkdir(exist_ok=True)
        self.case = Path(tempfile.mkdtemp(prefix="path-safety-", dir=runs))
        self.root = self.case / "root"
        self.root.mkdir()

    def test_existing_root_identity_and_item_contracts(self):
        item = self.root / "document.txt"
        item.write_text("sample", encoding="utf-8")
        guard = self.module.RootGuard(self.root)
        self.assertEqual(guard.original, self.module.identity(self.root.lstat()))
        self.assertEqual(guard.item("document.txt", "file").st_size, 6)
        self.assertIsNone(guard.item("missing.txt", "file"))
        with self.assertRaises(self.module.OperationError) as raised:
            guard.item("document.txt", "directory")
        self.assertEqual(raised.exception.code, "invalid_item")

    def test_replaced_root_is_rejected(self):
        guard = self.module.RootGuard(self.root)
        self.root.rename(self.case / "original-root")
        self.root.mkdir()
        with self.assertRaises(self.module.OperationError) as raised:
            guard.check()
        self.assertEqual(raised.exception.code, "root_changed")

    def test_parent_traversal_is_rejected_before_normalization(self):
        with self.assertRaises(self.module.OperationError) as raised:
            self.module.RootGuard(self.root / ".." / "root")
        self.assertEqual(raised.exception.code, "unsafe_path")

    def test_fail_uses_neutral_operation_error(self):
        with self.assertRaises(self.module.OperationError) as raised:
            self.module.fail("example", "message")
        self.assertEqual(raised.exception.code, "example")
        self.assertEqual(str(raised.exception), "message")


if __name__ == "__main__":
    unittest.main(verbosity=2)
