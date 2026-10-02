"""Filesystem behavior tests; fixtures stay under the system temporary test directory/inventory-*.

Mutations caught: hidden/nested omissions, following links, reading bodies,
writing caches, wrong totals, silent limits/errors, and invalid argument defaults.
"""
from __future__ import annotations

import ctypes
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
TEST_HOME = Path(__file__).resolve().parent
SOURCE = TEST_HOME.parent / "skills/project-directory-organizer/scripts"


class InventoryTests(unittest.TestCase):
    def setUp(self):
        run_home = Path(tempfile.gettempdir()) / "project-directory-organizer-tests"
        run_home.mkdir(exist_ok=True)
        self.case = Path(tempfile.mkdtemp(prefix="inventory-", dir=run_home))
        self.assertTrue(self.case.resolve().is_relative_to(run_home.resolve()))
        self.root = self.case / "target"
        self.root.mkdir()
        self.bundle = self.case / "scripts"
        self.bundle.mkdir()
        self.script = self.bundle / "folder_inventory.py"
        for name in ("folder_inventory.py", "path_safety.py"):
            if (SOURCE / name).exists():
                shutil.copyfile(SOURCE / name, self.bundle / name)

    def call(self, *arguments, root=None, success=True, bytecode_flag=True):
        command = [sys.executable]
        if bytecode_flag:
            command.append("-B")
        command.extend([str(self.script), str(root or self.root), *map(str, arguments)])
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            self.fail("Expected JSON result: " + result.stdout + result.stderr)

    def file(self, relative, data=b"sample"):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    @staticmethod
    def items(report):
        return {item["path"]: item for item in report["entries"]}

    def load_module(self):
        self.assertTrue(self.script.exists(), "Directory inventory feature is missing")
        sys.path.insert(0, str(self.bundle))
        try:
            spec = importlib.util.spec_from_file_location("folder_inventory_under_test", self.script)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        finally:
            sys.path.pop(0)

    @staticmethod
    def link(link, target):
        if os.name == "nt":
            environment = dict(os.environ, INVENTORY_TEST_LINK=str(link), INVENTORY_TEST_TARGET=str(target))
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "New-Item -ItemType Junction -Path $env:INVENTORY_TEST_LINK -Target $env:INVENTORY_TEST_TARGET | Out-Null"],
                capture_output=True, text=True, env=environment,
            )
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)
        else:
            link.symlink_to(target, target_is_directory=True)

    def test_empty_folder_has_complete_zero_inventory(self):
        report = self.call()
        self.assertTrue(report["complete"])
        self.assertEqual(report["entries"], [])
        self.assertEqual(report["summary"]["files"], 0)
        self.assertEqual(report["summary"]["directories"], 0)
        self.assertEqual(report["summary"]["file_bytes"], 0)

    def test_recursive_hidden_non_ascii_and_binary_metadata(self):
        self.file(".hidden", b"ab")
        self.file("资料/第二层/输入.bin", b"\x00\xff\xfe")
        self.file("unknown/not-a-standard-category/note.md", b"four")
        report = self.call()
        entries = self.items(report)
        self.assertTrue(report["complete"])
        self.assertEqual(set(entries), {".hidden", "资料", "资料/第二层", "资料/第二层/输入.bin",
                                        "unknown", "unknown/not-a-standard-category", "unknown/not-a-standard-category/note.md"})
        self.assertEqual(entries["资料/第二层/输入.bin"]["depth"], 3)
        self.assertEqual(entries["资料/第二层/输入.bin"]["size_bytes"], 3)
        self.assertRegex(entries[".hidden"]["modified_utc"], r"^\d{4}-\d\d-\d\dT.*Z$")
        self.assertEqual(report["summary"]["files"], 3)
        self.assertEqual(report["summary"]["directories"], 4)
        self.assertEqual(report["summary"]["file_bytes"], 9)

    @unittest.skipUnless(os.name == "nt", "NTFS preserves unpaired UTF-16 surrogate names")
    def test_unpaired_surrogate_filename_is_preserved_in_json(self):
        name = "unpaired-" + chr(0xDCFF)
        self.file(name, b"odd-name")
        report = self.call()
        self.assertTrue(report["complete"])
        self.assertEqual(set(self.items(report)), {name})
        self.assertEqual(self.items(report)[name]["size_bytes"], 8)

    def test_does_not_change_files_or_create_cache_without_dash_b(self):
        self.file("library/original.pdf", b"original bytes")
        before = {str(p.relative_to(self.case)): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.case.rglob("*") if p.is_file()}
        self.call(bytecode_flag=False)
        self.call("--summary", bytecode_flag=False)
        after = {str(p.relative_to(self.case)): (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in self.case.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(any(p.name == "__pycache__" for p in self.case.rglob("*")))

    @unittest.skipUnless(os.name == "nt", "Windows shared metadata/no-content lock characterization")
    def test_file_content_locked_still_reports_metadata(self):
        path = self.file("locked-content.bin", b"not available for content reading")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel.CreateFileW
        create_file.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                               ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        create_file.restype = ctypes.c_void_p
        # A read handle without sharing allows metadata queries but denies a second body open.
        handle = create_file(str(path), 0x80000000, 0, None, 3, 0x80, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value, ctypes.get_last_error())
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        try:
            with self.assertRaises(PermissionError):
                path.read_bytes()
            report = self.call()
            self.assertTrue(report["complete"])
            self.assertEqual(self.items(report)["locked-content.bin"]["size_bytes"], 33)
        finally:
            kernel.CloseHandle(handle)

    def test_generated_names_are_case_insensitively_skipped_and_counted(self):
        for name in (".GiT", "node_modules", ".venv", "venv", "__pycache__", ".cache"):
            self.file(name + "/unlisted.txt", b"large")
        self.file("src/code.py", b"s")
        self.file("build/app.exe", b"bb")
        self.file("dist/final.zip", b"ddd")
        self.file("output/report.md", b"rrrr")
        report = self.call()
        entries = self.items(report)
        self.assertFalse(report["complete"])
        self.assertEqual(len(report["omissions"]), 6)
        self.assertEqual(report["summary"]["file_bytes"], 10)
        self.assertEqual(report["summary"]["files"], 4)
        for name in (".GiT", "node_modules", ".venv", "venv", "__pycache__", ".cache"):
            self.assertIn(name, entries)
            self.assertNotIn(name + "/unlisted.txt", entries)
            self.assertEqual(entries[name]["children_status"], "skipped_generated")

    def test_include_generated_expands_dependencies(self):
        self.file("node_modules/package/index.js", b"abc")
        report = self.call("--include-generated")
        self.assertTrue(report["complete"])
        self.assertIn("node_modules/package/index.js", self.items(report))
        self.assertEqual(report["summary"]["file_bytes"], 3)

    def test_max_depth_one_lists_only_top_level_and_reports_unknown_descendants(self):
        self.file("top.md", b"a")
        self.file("nested/child.md", b"bbb")
        report = self.call("--max-depth", 1)
        self.assertEqual(set(self.items(report)), {"top.md", "nested"})
        self.assertFalse(report["complete"])
        self.assertEqual(self.items(report)["nested"]["children_status"], "skipped_depth")
        self.assertEqual(report["summary"]["file_bytes"], 1)
        self.assertEqual(report["limits"]["max_depth"], 1)

    def test_max_entries_stops_and_does_not_claim_unknown_total(self):
        for index in range(8):
            self.file("file-" + str(index) + ".txt", b"abc")
        report = self.call("--max-entries", 3)
        self.assertEqual(len(report["entries"]), 3)
        self.assertEqual(report["summary"]["files"], 3)
        self.assertEqual(report["summary"]["file_bytes"], 9)
        self.assertFalse(report["complete"])
        self.assertTrue(any(item["reason"] == "max_entries" for item in report["omissions"]))
        self.assertIsNone(report["summary"]["unlisted_entries"])

    def test_forty_nested_folders_are_not_silently_depth_limited(self):
        relative = "/".join(["d"] * 40) + "/leaf"
        self.file(relative, b"x")
        report = self.call()
        self.assertTrue(report["complete"])
        self.assertEqual(self.items(report)[relative]["depth"], 41)

    def test_child_junction_is_reported_without_target_contents(self):
        outside = self.case / "outside"
        outside.mkdir()
        (outside / "private.txt").write_bytes(b"private")
        self.link(self.root / "linked", outside)
        self.file("normal.txt", b"ok")
        report = self.call("--include-generated")
        self.assertEqual(set(self.items(report)), {"linked", "normal.txt"})
        self.assertEqual(self.items(report)["linked"]["type"], "reparse_point")
        self.assertFalse(report["complete"])
        self.assertEqual(report["summary"]["file_bytes"], 2)

    def test_junction_cycle_does_not_recurse(self):
        self.link(self.root / "loop", self.root)
        report = self.call()
        self.assertEqual(set(self.items(report)), {"loop"})
        self.assertFalse(report["complete"])

    def test_root_junction_is_rejected(self):
        link = self.case / "root-link"
        self.link(link, self.root)
        report = self.call(root=link, success=False)
        self.assertEqual(report["code"], "reparse_point")

    def test_ancestor_junction_is_rejected(self):
        child = self.root / "child"
        child.mkdir()
        link = self.case / "ancestor-link"
        self.link(link, self.root)
        report = self.call(root=link / "child", success=False)
        self.assertEqual(report["code"], "reparse_point")

    def test_missing_root_and_file_root_return_json_error(self):
        report = self.call(root=self.case / "missing", success=False)
        self.assertEqual(report["code"], "missing_root")
        path = self.file("file")
        report = self.call(root=path, success=False)
        self.assertEqual(report["code"], "not_directory")

    def test_invalid_limits_are_rejected_instead_of_silently_changed(self):
        for option, value in (("--max-depth", "0"), ("--max-depth", "-1"), ("--max-depth", "many"),
                              ("--max-entries", "0"), ("--max-entries", "-3"), ("--max-entries", "1.5")):
            with self.subTest(option=option, value=value):
                report = self.call(option, value, success=False)
                self.assertEqual(report["code"], "arguments")

    def test_read_error_is_partial_and_other_real_siblings_remain_visible(self):
        self.file("blocked/secret.txt")
        self.file("readable/ok.txt", b"ok")
        module = self.load_module()
        real_scandir = os.scandir

        def deny_one(path):
            # Windows ACL effects depend on user privileges; inject only this OS
            # failure, keeping both tree traversal and other siblings real.
            if Path(path) == self.root / "blocked":
                raise PermissionError(13, "test directory access denied", str(path))
            return real_scandir(path)

        with patch.object(module.os, "scandir", side_effect=deny_one):
            report = module.inventory(self.root)
        self.assertFalse(report["complete"])
        self.assertIn("readable/ok.txt", self.items(report))
        self.assertNotIn("blocked/secret.txt", self.items(report))
        self.assertTrue(any(error["path"] == "blocked" for error in report["errors"]))
        self.assertEqual(self.items(report)["blocked"]["children_status"], "error")

    def test_node_metadata_error_keeps_other_files_and_marks_count_scope(self):
        blocked = self.file("blocked.bin", b"xxx")
        self.file("readable.txt", b"ok")
        module = self.load_module()
        real_lstat = Path.lstat

        def deny_one(path):
            if path == blocked:
                raise PermissionError(13, "test metadata unavailable", str(path))
            return real_lstat(path)

        with patch.object(Path, "lstat", deny_one):
            report = module.inventory(self.root)
        self.assertFalse(report["complete"])
        self.assertEqual(self.items(report)["blocked.bin"]["type"], "unavailable")
        self.assertEqual(self.items(report)["readable.txt"]["type"], "file")
        self.assertEqual(report["summary"]["files"], 1)
        self.assertEqual(report["summary"]["file_bytes"], 2)
        self.assertIsNone(report["summary"]["unlisted_entries"])

    def test_entry_limit_does_not_open_pending_subtrees(self):
        self.file("deep/child/file.bin")
        module = self.load_module()
        real_scandir = os.scandir

        def root_only(path):
            if Path(path) != self.root:
                raise AssertionError("A subtree was opened after the entry limit")
            return real_scandir(path)

        with patch.object(module.os, "scandir", side_effect=root_only):
            report = module.inventory(self.root, max_entries=1)
        self.assertEqual(set(self.items(report)), {"deep"})
        self.assertEqual(self.items(report)["deep"]["children_status"], "skipped_limit")
        self.assertFalse(report["complete"])

    def summarize(self, report, top=10):
        module = self.load_module()
        render = getattr(module, "summarize", None)
        self.assertTrue(callable(render), "Pure summary renderer is missing")
        return render(report, top=top)

    def test_summary_groups_direct_children_and_extensions_without_losing_equal_names(self):
        self.file("alpha/deep/same.TXT", b"12345")
        self.file("beta/same.txt", b"12345")
        self.file("beta/archive.tar.gz", b"12")
        self.file(".hidden", b"abc")
        self.file("README", b"x")
        report = self.call()
        result = self.summarize(report, top=2)
        self.assertNotIn("entries", result)
        groups = {item["path"]: item for item in result["summary"]["by_top_level"]}
        self.assertEqual(set(groups), {".", "alpha", "beta"})
        self.assertEqual((groups["alpha"]["entries"], groups["alpha"]["files"],
                          groups["alpha"]["directories"], groups["alpha"]["file_bytes"]), (3, 1, 2, 5))
        self.assertEqual((groups["beta"]["files"], groups["beta"]["file_bytes"]), (2, 7))
        self.assertEqual((groups["."]["files"], groups["."]["file_bytes"]), (2, 4))
        extensions = {item["extension"]: (item["files"], item["file_bytes"])
                      for item in result["summary"]["by_extension"]}
        self.assertEqual(extensions, {".txt": (2, 10), ".gz": (1, 2), "": (2, 4)})
        self.assertEqual([item["path"] for item in result["summary"]["largest_files"]],
                         ["alpha/deep/same.TXT", "beta/same.txt"])
        self.assertEqual(result["summary"]["top"], 2)

    def test_summary_preserves_partial_status_hidden_and_generated_scope(self):
        self.file(".hidden", b"abc")
        self.file("node_modules/omitted.js", b"not counted")
        self.file("nested/omitted.txt", b"also not counted")
        report = self.call("--max-depth", 1)
        result = self.summarize(report)
        for key in report.keys() - {"entries", "summary"}:
            self.assertEqual(result[key], report[key], key)
        for key, value in report["summary"].items():
            self.assertEqual(result["summary"][key], value, key)
        self.assertFalse(result["complete"])
        self.assertIsNone(result["summary"]["unlisted_entries"])
        self.assertTrue(result["policy"]["include_hidden"])
        self.assertFalse(result["policy"]["include_generated"])
        self.assertEqual(result["summary"]["files"], 1)
        self.assertEqual(result["summary"]["file_bytes"], 3)
        self.assertEqual({item["reason"] for item in result["omissions"]},
                         {"generated_directory", "max_depth"})

    def test_summary_uses_finished_time_for_exact_age_boundaries_future_and_unknown(self):
        report = self.call()
        finished = datetime(2026, 10, 1, tzinfo=timezone.utc)
        report["finished_utc"] = "2026-10-01T00:00:00Z"
        samples = [(-1, "future"), (0, "<7d"), (7 - 1 / 86400, "<7d"),
                   (7, "7-30d"), (30 - 1 / 86400, "7-30d"), (30, "30-90d"),
                   (90 - 1 / 86400, "30-90d"), (90, "90-365d"),
                   (365 - 1 / 86400, "90-365d"), (365, ">=365d")]
        report["entries"] = [
            {"path": str(index) + ".txt", "type": "file", "depth": 1, "size_bytes": 2,
             "modified_utc": (finished - timedelta(days=age)).isoformat()}
            for index, (age, _) in enumerate(samples)
        ]
        for index, value in enumerate((None, "invalid", "2026-09-30T00:00:00")):
            report["entries"].append({"path": "unknown-" + str(index), "type": "file",
                                      "depth": 1, "size_bytes": 3, "modified_utc": value})
        report["summary"].update(entries=13, files=13, file_bytes=29)
        result = self.summarize(report)
        buckets = {item["age"]: (item["files"], item["file_bytes"])
                   for item in result["summary"]["by_modified_age"]}
        self.assertEqual(buckets, {"<7d": (2, 4), "7-30d": (2, 4), "30-90d": (2, 4),
                                   "90-365d": (2, 4), ">=365d": (1, 2),
                                   "future": (1, 2), "unknown": (3, 9)})
        self.assertEqual(result["summary"]["reference_utc"], report["finished_utc"])

    def test_summary_recent_files_sort_by_instant_and_exclude_future_unknown(self):
        report = self.call()
        report["finished_utc"] = "2026-10-01T00:00:00Z"
        samples = [("a/old.txt", "2026-09-30T02:00:00+02:00"),
                   ("z/new.txt", "2026-09-30T01:00:00Z"),
                   ("future.txt", "2026-10-02T00:00:00Z"), ("unknown.txt", None)]
        report["entries"] = [{"path": path, "type": "file", "depth": 2,
                              "size_bytes": 1, "modified_utc": modified} for path, modified in samples]
        report["summary"].update(entries=4, files=4, file_bytes=4)
        result = self.summarize(report)
        self.assertEqual([item["path"] for item in result["summary"]["recently_modified_files"]],
                         ["z/new.txt", "a/old.txt"])

    def test_summary_is_pure_metadata_rendering_after_source_tree_is_removed(self):
        self.file("a/same.bin", b"identical")
        self.file("b/same.bin", b"identical")
        report = self.call()
        before = deepcopy(report)
        # The renderer must work without the original tree or any accessible body.
        for target in sorted(self.root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if target.is_file():
                target.unlink()
            else:
                target.rmdir()
        self.root.rmdir()
        module = self.load_module()
        self.assertTrue(callable(getattr(module, "summarize", None)), "Pure summary renderer is missing")
        with patch.object(module.os, "scandir", side_effect=AssertionError("Unexpected second traversal")), \
                patch("builtins.open", side_effect=AssertionError("Unexpected content or cache access")):
            result = module.summarize(report)
        self.assertEqual(report, before)
        self.assertEqual(result["summary"]["files"], 2)
        self.assertEqual(len(result["summary"]["largest_files"]), 2)

    def test_summary_cli_default_compatibility_zero_files_and_top_limit(self):
        empty = self.call("--summary")
        self.assertNotIn("entries", empty)
        self.assertEqual(empty["summary"]["files"], 0)
        self.assertEqual(empty["summary"]["by_top_level"], [])
        self.assertEqual(empty["summary"]["by_extension"], [])
        self.assertEqual(empty["summary"]["largest_files"], [])
        self.assertEqual(empty["summary"]["recently_modified_files"], [])
        self.assertEqual(sum(item["files"] for item in empty["summary"]["by_modified_age"]), 0)
        for index in range(12):
            self.file("file-" + str(index) + ".bin", b"x" * (index + 1))
        default = self.call()
        self.assertIn("entries", default)
        self.assertNotIn("by_top_level", default["summary"])
        unchanged = self.call("--top", 1)
        self.assertEqual(unchanged["summary"], default["summary"])
        result = self.call("--summary")
        self.assertEqual(len(result["summary"]["largest_files"]), 10)
        self.assertEqual(len(result["summary"]["recently_modified_files"]), 10)
        limited = self.call("--summary", "--top", 1)
        self.assertEqual([item["path"] for item in limited["summary"]["largest_files"]], ["file-11.bin"])
        self.assertEqual(limited["summary"]["files"], 12)

    def test_summary_cli_and_api_reject_non_positive_or_non_integer_top(self):
        for value in ("0", "-1", "many", "1.5"):
            with self.subTest(cli=value):
                result = self.call("--summary", "--top", value, success=False)
                self.assertEqual(result["code"], "arguments")
        module = self.load_module()
        self.assertTrue(callable(getattr(module, "summarize", None)), "Pure summary renderer is missing")
        report = self.call()
        for value in (0, -1, True, 1.5, "2", None):
            with self.subTest(api=value), self.assertRaises(module.OperationError) as error:
                module.summarize(report, top=value)
            self.assertEqual(error.exception.code, "arguments")

    def test_summary_entry_limit_remains_partial_without_opening_subtrees(self):
        self.file("deep/child/file.bin")
        result = self.call("--summary", "--max-entries", 1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["summary"]["entries"], 1)
        self.assertEqual(result["summary"]["files"], 0)
        self.assertEqual(result["limits"]["max_entries"], 1)
        self.assertEqual(result["omissions"], [{"path": ".", "reason": "max_entries", "unlisted_entries": None}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
