"""Real filesystem tests; retained fixtures are bounded to the system temporary test directory/delete-*.

No cleanup removes fixtures: failures remain inspectable. All destructive CLI
calls receive a fixture root proven to stay within that dedicated test area.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid

sys.dont_write_bytecode = True
TEST_HOME = Path(__file__).resolve().parent
SCRIPT = TEST_HOME.parent / "skills/project-directory-organizer/scripts/safe_delete.py"


class SafeDeleteTests(unittest.TestCase):
    def setUp(self):
        self.runs = Path(tempfile.gettempdir()) / "project-directory-organizer-tests"
        self.runs.mkdir(exist_ok=True)
        self.case = Path(tempfile.mkdtemp(prefix="delete-", dir=self.runs))
        self.assertTrue(self.case.resolve().is_relative_to(self.runs.resolve()))
        self.root = self.case / "root"
        self.root.mkdir()

    def call(self, command, *args, success=True, root=None):
        root = self.root if root is None else Path(root)
        # Resolve only normal fixture roots here. Link fixtures are themselves
        # inside the test area and may point only at another fixture directory.
        self.assertTrue(root.absolute().is_relative_to(self.case.absolute()))
        self.assertTrue(root.resolve().is_relative_to(self.case.resolve()))
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), command, str(root), *map(str, args)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        try:
            return json.loads(result.stdout)
        except ValueError:
            self.fail("CLI did not return JSON: " + result.stdout + result.stderr)

    def write(self, path, content="fixture"):
        destination = self.root / path
        self.assertTrue(destination.absolute().is_relative_to(self.root.absolute()))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        return destination

    def plan(self, *paths, **options):
        args = []
        for path in paths:
            args.extend(["--path", path])
        if options.pop("allow_library", False):
            args.append("--allow-library")
        maximum = options.pop("max_entries", None)
        if maximum is not None:
            args.extend(["--max-entries", maximum])
        return self.call("plan", *args, **options)

    def apply(self, plan, success=True, root=None):
        path = self.case / ("plan-" + uuid.uuid4().hex + ".json")
        path.write_text(json.dumps(plan, ensure_ascii=True), encoding="utf-8")
        return self.call("apply", "--plan-file", path, success=success, root=root)

    def batch(self, payload):
        return self.root / ".project-trash" / payload["batch"]

    def test_plan_reads_full_tree_without_creating_metadata(self):
        item = self.write("draft/sub/report.txt", "draft")
        before = item.stat().st_mtime_ns
        result = self.plan("draft")
        self.assertEqual(result["targets"][0]["path"], "draft")
        self.assertEqual({entry["path"] for entry in result["targets"][0]["entries"]}, {"", "sub", "sub/report.txt"})
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["draft"])
        self.assertEqual(item.stat().st_mtime_ns, before)

    def test_exact_files_and_directory_quarantine_then_restore(self):
        self.write("one.txt", "one")
        self.write("draft/sub/two.txt", "two")
        self.write("keep.txt", "keep")
        result = self.apply(self.plan("one.txt", "draft"))
        self.assertEqual(result["status"], "quarantined")
        self.assertFalse((self.root / "one.txt").exists())
        self.assertFalse((self.root / "draft").exists())
        self.assertEqual((self.root / "keep.txt").read_text(), "keep")
        restored = self.call("restore", "--batch", result["batch"])
        self.assertEqual(restored["status"], "restored")
        self.assertEqual((self.root / "one.txt").read_text(), "one")
        self.assertEqual((self.root / "draft/sub/two.txt").read_text(), "two")
        manifest = json.loads((self.batch(result) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "restored")

    @unittest.skipUnless(os.name == "nt", "Windows case-insensitive root paths")
    def test_root_case_variant_apply_restores_using_plan_root(self):
        item = self.write("one.txt", "unchanged content")
        variant = self.root.with_name("ROOT")
        self.assertTrue(self.root.samefile(variant))
        result = self.apply(self.plan("one.txt"), root=variant)
        owner = self.root / ".project-trash/owner.json"
        owner_before = owner.read_bytes()
        self.assertFalse(item.exists())

        restored = self.call("restore", "--batch", result["batch"])

        self.assertEqual(restored["status"], "restored")
        self.assertEqual(item.read_text(encoding="utf-8"), "unchanged content")
        self.assertEqual(owner.read_bytes(), owner_before)

    @unittest.skipUnless(os.name == "nt", "Windows case-insensitive root paths")
    def test_existing_quarantine_accepts_root_case_variant_without_rewriting_owner(self):
        self.write("one.txt", "first")
        first = self.apply(self.plan("one.txt"))
        owner = self.root / ".project-trash/owner.json"
        owner_before = owner.read_bytes()
        first_manifest = (self.batch(first) / "manifest.json").read_bytes()
        self.write("two.txt", "second")
        variant = self.root.with_name("ROOT")

        second = self.apply(self.plan("two.txt", root=variant), root=variant)

        self.assertEqual(second["status"], "quarantined")
        self.assertEqual(owner.read_bytes(), owner_before)
        self.assertEqual((self.batch(first) / "manifest.json").read_bytes(), first_manifest)
        self.assertFalse((self.root / "two.txt").exists())
        self.call("restore", "--batch", second["batch"], root=variant)
        self.call("restore", "--batch", first["batch"], root=variant)
        self.assertEqual((self.root / "one.txt").read_text(), "first")
        self.assertEqual((self.root / "two.txt").read_text(), "second")
        self.assertEqual(owner.read_bytes(), owner_before)

    @unittest.skipUnless(os.name == "nt", "Windows case-insensitive root paths")
    def test_root_case_variant_can_purge_existing_batch(self):
        self.write("one.txt", "selected")
        self.write("keep.txt", "untouched")
        result = self.apply(self.plan("one.txt"))
        owner = self.root / ".project-trash/owner.json"
        owner_before = owner.read_bytes()

        purged = self.call("purge", "--batch", result["batch"], "--permanent", root=self.root.with_name("ROOT"))

        self.assertEqual(purged["status"], "purged")
        self.assertEqual(list((self.batch(result) / "items").iterdir()), [])
        self.assertEqual((self.root / "keep.txt").read_text(), "untouched")
        self.assertEqual(owner.read_bytes(), owner_before)

    @unittest.skipUnless(os.name == "nt", "Windows case-insensitive root paths")
    def test_root_case_variant_still_rejects_invalid_quarantine_ownership(self):
        self.write("one.txt", "retained")
        result = self.apply(self.plan("one.txt"))
        owner_path = self.root / ".project-trash/owner.json"
        original_bytes = owner_path.read_bytes()
        original = json.loads(original_bytes)
        other_root = self.case / "different-root"
        other_root.mkdir()
        variants = [
            dict(original, schema_version=2),
            dict(original, tool="another-tool"),
            dict(original, root=str(other_root)),
            dict(original, root_identity=[original["root_identity"][0], original["root_identity"][1] + 1]),
            dict(original, root=[]),
            dict(original, unexpected=True),
            {key: value for key, value in original.items() if key != "root_identity"},
        ]
        for changed in variants:
            with self.subTest(owner=changed):
                altered = json.dumps(changed).encode("utf-8")
                owner_path.write_bytes(altered)

                rejected = self.call("restore", "--batch", result["batch"], root=self.root.with_name("ROOT"), success=False)

                self.assertEqual(rejected["code"], "invalid_trash")
                self.assertFalse((self.root / "one.txt").exists())
                self.assertEqual((self.batch(result) / "items/0000").read_text(), "retained")
                self.assertEqual(owner_path.read_bytes(), altered)
        owner_path.write_bytes(original_bytes)
        self.call("restore", "--batch", result["batch"])
        self.assertEqual((self.root / "one.txt").read_text(), "retained")

    def test_library_original_requires_explicit_flag_in_plan(self):
        original = self.write("library/topic/original.txt", "unchanged")
        self.plan("library/topic/original.txt", success=False)
        self.assertEqual(original.read_text(), "unchanged")
        plan = self.plan("library/topic/original.txt", allow_library=True)
        self.assertTrue(plan["allow_library"])
        result = self.apply(plan)
        self.call("restore", "--batch", result["batch"])
        self.assertEqual(original.read_text(), "unchanged")

    def test_library_root_and_ancestor_require_explicit_flag(self):
        self.write("library/topic/original.txt", "original")
        for root, selected in ((self.root / "library", "topic/original.txt"), (self.root / "library/topic", "original.txt")):
            with self.subTest(root=root):
                self.call("plan", "--path", selected, root=root, success=False)
                allowed = self.call("plan", "--path", selected, "--allow-library", root=root)
                self.assertTrue(allowed["allow_library"])

    def test_parent_of_nested_library_requires_explicit_flag(self):
        self.write("project/library/raw.txt", "raw")
        self.plan("project", success=False)
        allowed = self.plan("project", allow_library=True)
        self.assertTrue(allowed["allow_library"])

    def test_library_root_cannot_receive_mutation_journals(self):
        self.write("library/original.txt", "original")
        library = self.root / "library"
        plan = self.call("plan", "--path", "original.txt", "--allow-library", root=library)
        record = self.case / "library-plan.json"
        record.write_text(json.dumps(plan), encoding="utf-8")
        self.call("apply", "--plan-file", record, root=library, success=False)
        self.assertEqual(sorted(p.name for p in library.iterdir()), ["original.txt"])

    def test_quarantine_root_and_ancestor_cannot_be_rebased(self):
        self.write(".project-trash/batch/keep.txt", "keep")
        for root, selected in ((self.root / ".project-trash", "batch/keep.txt"), (self.root / ".project-trash/batch", "keep.txt")):
            with self.subTest(root=root):
                self.call("plan", "--path", selected, root=root, success=False)

    def test_parent_of_nested_quarantine_refused(self):
        self.write("project/.project-trash/keep.txt", "keep")
        self.plan("project", success=False)
        self.plan("project/.project-trash/keep.txt", success=False)

    def test_non_windows_mutations_refused_before_any_changes(self):
        self.write("one.txt", "keep")
        code = "import runpy,sys; script=sys.argv[1]; sys.path.insert(0,str(__import__('pathlib').Path(script).parent)); sys.argv=sys.argv[1:]; sys.platform='linux'; runpy.run_path(script,run_name='__main__')"
        for args in (("apply", "--plan-file", "unused.json"), ("restore", "--batch", "0" * 32), ("purge", "--batch", "0" * 32, "--permanent")):
            result = subprocess.run([sys.executable, "-B", "-c", code, str(SCRIPT), args[0], str(self.root), *args[1:]], capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["code"], "unsupported_platform")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["one.txt"])

    @unittest.skipUnless(os.name == "nt", "NTFS permits unpaired UTF-16 surrogates")
    def test_surrogate_filename_is_quarantined_and_restored(self):
        name = "odd-\ud800.txt"
        item = self.write(name, "keep bytes")
        plan = self.plan(name)
        result = self.apply(plan)
        self.assertFalse(item.exists())
        self.call("restore", "--batch", result["batch"])
        self.assertEqual(item.read_text(), "keep bytes")

    @unittest.skipUnless(os.name == "nt", "NTFS permits unpaired UTF-16 surrogates")
    def test_surrogate_root_round_trips_owner_and_manifest(self):
        self.root = self.case / "root-\ud800"
        self.root.mkdir()
        item = self.write("one.txt", "keep bytes")
        result = self.apply(self.plan("one.txt"))
        self.assertFalse(item.exists())
        owner = json.loads((self.root / ".project-trash/owner.json").read_text(encoding="utf-8"))
        self.assertEqual(owner["root"], str(self.root))
        self.call("restore", "--batch", result["batch"])
        self.assertEqual(item.read_text(), "keep bytes")

    def test_changed_second_target_prevents_all_moves(self):
        first = self.write("first.txt", "first")
        second = self.write("second.txt", "second")
        plan = self.plan("first.txt", "second.txt")
        second.write_text("changed", encoding="utf-8")
        self.apply(plan, success=False)
        self.assertTrue(first.exists())
        self.assertEqual(second.read_text(), "changed")
        self.assertFalse((self.root / ".project-trash").exists())

    def test_added_descendant_invalidates_plan(self):
        self.write("draft/a.txt")
        plan = self.plan("draft")
        self.write("draft/unknown.txt", "new")
        self.apply(plan, success=False)
        self.assertTrue((self.root / "draft/a.txt").exists())
        self.assertTrue((self.root / "draft/unknown.txt").exists())

    def test_changed_content_with_preserved_size_and_time_refused(self):
        item = self.write("report.txt", "AAAA")
        plan = self.plan("report.txt")
        info = item.stat()
        item.write_text("BBBB", encoding="utf-8")
        os.utime(item, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.apply(plan, success=False)
        self.assertEqual(item.read_text(), "BBBB")

    def test_unsafe_literal_targets_refused(self):
        self.write("keep.txt")
        for path in (".", "..", "../root/keep.txt", str(self.root / "keep.txt"), "*.txt", "foo/../keep.txt", ".project-trash", ".project-trash/batch", ".project-delete.lock"):
            with self.subTest(path=path):
                self.plan(path, success=False)
        self.assertEqual((self.root / "keep.txt").read_text(), "fixture")

    def test_overlapping_or_duplicate_targets_refused(self):
        self.write("draft/a.txt")
        self.plan("draft", "draft/a.txt", success=False)
        self.plan("draft", "draft", success=False)

    def short_name(self, path):
        """Get a real NTFS short alias; do not invent a name if 8.3 is disabled."""
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        get_short = kernel.GetShortPathNameW
        get_short.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        get_short.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = get_short(str(path), buffer, len(buffer))
        self.assertGreater(length, 0, ctypes.get_last_error())
        self.assertLess(length, len(buffer))
        name = Path(buffer.value).name
        if name.casefold() == path.name.casefold():
            self.skipTest("This volume did not generate an 8.3 alias for this fixture")
        return name

    @unittest.skipUnless(os.name == "nt", "Windows short filenames")
    def test_quarantine_short_name_target_is_protected(self):
        self.write("one.txt", "keep")
        result = self.apply(self.plan("one.txt"))
        short = self.short_name(self.root / ".project-trash")
        rejected = self.plan(short + "/owner.json", success=False)
        self.assertEqual(rejected["code"], "protected_path")
        self.assertTrue((self.root / ".project-trash/owner.json").is_file())
        self.call("restore", "--batch", result["batch"])
        self.assertEqual((self.root / "one.txt").read_text(), "keep")

    @unittest.skipUnless(os.name == "nt", "Windows short filenames")
    def test_quarantine_short_name_root_is_protected(self):
        self.write("one.txt", "keep")
        result = self.apply(self.plan("one.txt"))
        short = self.short_name(self.root / ".project-trash")
        rejected = self.call("plan", "--path", "owner.json", root=self.root / short, success=False)
        self.assertEqual(rejected["code"], "protected_path")
        self.call("restore", "--batch", result["batch"])
        self.assertEqual((self.root / "one.txt").read_text(), "keep")

    @unittest.skipUnless(os.name == "nt", "Windows short filenames")
    def test_long_and_short_names_of_same_target_are_rejected(self):
        item = self.write("long filename.txt", "keep")
        short = self.short_name(item)
        rejected = self.plan(item.name, short, success=False)
        self.assertEqual(rejected["code"], "overlap")
        self.assertEqual(item.read_text(), "keep")
        self.assertFalse((self.root / ".project-trash").exists())

    @unittest.skipUnless(os.name == "nt", "Windows short filenames")
    def test_long_and_short_names_of_parent_child_targets_are_rejected(self):
        item = self.write("long directory/child.txt", "keep")
        short = self.short_name(item.parent)
        for paths in ((item.parent.name, short + "/child.txt"), (short, "long directory/child.txt")):
            with self.subTest(paths=paths):
                rejected = self.plan(*paths, success=False)
                self.assertEqual(rejected["code"], "overlap")
        self.assertEqual(item.read_text(), "keep")
        self.assertFalse((self.root / ".project-trash").exists())

    @unittest.skipUnless(os.name == "nt", "Windows short filenames")
    def test_short_name_target_restores_original_chinese_name(self):
        item = self.write("分析结果汇总~最终稿.txt", "keep Chinese name and tilde")
        short = self.short_name(item)
        result = self.apply(self.plan(short))
        self.call("restore", "--batch", result["batch"])
        self.assertEqual(item.read_text(encoding="utf-8"), "keep Chinese name and tilde")
        self.assertIn(item.name, {path.name for path in self.root.iterdir()})

    @unittest.skipUnless(os.name == "nt", "Windows short filenames")
    def test_saved_plan_short_name_alias_requires_a_fresh_plan(self):
        item = self.write("long filename.txt", "keep")
        short = self.short_name(item)
        plan = self.plan(item.name)
        plan["targets"][0]["path"] = short
        rejected = self.apply(plan, success=False)
        self.assertEqual(rejected["code"], "invalid_plan")
        self.assertEqual(item.read_text(), "keep")
        self.assertFalse((self.root / ".project-trash").exists())

    @unittest.skipUnless(os.name == "nt", "Windows short filenames")
    def test_saved_plan_cannot_select_quarantine_by_short_name(self):
        self.write("one.txt", "keep")
        self.apply(self.plan("one.txt"))
        short = self.short_name(self.root / ".project-trash")
        self.write("two.txt", "second")
        plan = self.plan("two.txt")
        plan["targets"][0]["path"] = short + "/owner.json"
        owner = self.root / ".project-trash/owner.json"
        info = owner.stat()
        plan["targets"][0]["entries"] = [{
            "path": "", "kind": "file", "identity": [info.st_dev, info.st_ino],
            "size": info.st_size, "mtime_ns": info.st_mtime_ns,
            "sha256": hashlib.sha256(owner.read_bytes()).hexdigest(),
        }]
        rejected = self.apply(plan, success=False)
        self.assertEqual(rejected["code"], "protected_path")
        self.assertTrue((self.root / ".project-trash/owner.json").is_file())
        self.assertEqual((self.root / "two.txt").read_text(), "second")

    @unittest.skipUnless(os.name == "nt", "Windows short filenames")
    def test_restore_cannot_use_quarantine_short_name_as_destination_parent(self):
        self.write("one.txt", "keep")
        result = self.apply(self.plan("one.txt"))
        short = self.short_name(self.root / ".project-trash")
        record = self.batch(result) / "manifest.json"
        manifest = json.loads(record.read_text(encoding="utf-8"))
        manifest["plan"]["targets"][0]["path"] = short + "/misplaced.txt"
        record.write_text(json.dumps(manifest), encoding="utf-8")
        rejected = self.call("restore", "--batch", result["batch"], success=False)
        self.assertEqual(rejected["code"], "protected_path")
        self.assertEqual((self.batch(result) / "items/0000").read_text(), "keep")
        self.assertFalse((self.root / ".project-trash/misplaced.txt").exists())

    def test_entry_limit_fails_without_executable_truncated_plan(self):
        self.write("draft/a.txt")
        self.write("draft/b.txt")
        result = self.plan("draft", max_entries=2, success=False)
        self.assertNotIn("targets", result)
        self.assertFalse((self.root / ".project-trash").exists())

    def test_restore_collision_leaves_both_versions(self):
        self.write("one.txt", "old")
        result = self.apply(self.plan("one.txt"))
        self.write("one.txt", "new")
        self.call("restore", "--batch", result["batch"], success=False)
        self.assertEqual((self.root / "one.txt").read_text(), "new")
        self.assertEqual((self.batch(result) / "items/0000").read_text(), "old")

    def test_lock_conflict_prevents_mutation(self):
        item = self.write("one.txt")
        plan = self.plan("one.txt")
        lock = self.write(".project-delete.lock", "another owner")
        self.apply(plan, success=False)
        self.assertEqual(lock.read_text(), "another owner")
        self.assertTrue(item.exists())

    def test_schema_and_path_tampering_are_rejected(self):
        item = self.write("one.txt")
        original = self.plan("one.txt")
        for key, value in (("schema_version", 900), ("allow_library", "yes")):
            with self.subTest(key=key):
                changed = dict(original, **{key: value})
                self.apply(changed, success=False)
        changed = json.loads(json.dumps(original))
        changed["targets"][0]["path"] = "../outside.txt"
        self.apply(changed, success=False)
        changed = json.loads(json.dumps(original))
        changed["targets"][0]["entries"][0]["path"] = "../outside.txt"
        self.apply(changed, success=False)
        self.assertTrue(item.exists())

    def test_permanent_purge_requires_flag_then_removes_only_batch_items(self):
        self.write("draft/a.txt", "remove")
        self.write("keep.txt", "keep")
        result = self.apply(self.plan("draft"))
        self.call("purge", "--batch", result["batch"], success=False)
        self.assertTrue((self.batch(result) / "items/0000/a.txt").exists())
        purge = self.call("purge", "--batch", result["batch"], "--permanent")
        self.assertEqual(purge["status"], "purged")
        self.assertEqual(list((self.batch(result) / "items").iterdir()), [])
        self.assertTrue((self.batch(result) / "manifest.json").exists())
        self.assertEqual((self.root / "keep.txt").read_text(), "keep")

    def test_unknown_quarantine_content_blocks_purge(self):
        self.write("draft/a.txt")
        result = self.apply(self.plan("draft"))
        unknown = self.batch(result) / "items/0000/new.txt"
        unknown.write_text("keep", encoding="utf-8")
        self.call("purge", "--batch", result["batch"], "--permanent", success=False)
        self.assertTrue((self.batch(result) / "items/0000/a.txt").exists())
        self.assertEqual(unknown.read_text(), "keep")

    def make_link(self, path, target, directory=True):
        self.assertTrue(path.absolute().is_relative_to(self.case.absolute()))
        self.assertTrue(target.resolve().is_relative_to(self.case.resolve()))
        if os.name == "nt" and directory:
            # Junction creation needs no elevated privilege; PowerShell owns the
            # whole operation, with exact literal fixture paths and no deletion.
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "New-Item -ItemType Junction -Path " + quote(path) + " -Target " + quote(target) + " -ErrorAction Stop | Out-Null"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            try:
                path.symlink_to(target, target_is_directory=directory)
            except (OSError, NotImplementedError) as error:
                self.skipTest("This filesystem does not permit this symlink fixture: " + str(error))

    def test_junction_selected_root_refused(self):
        real = self.case / "real"
        real.mkdir()
        (real / "keep.txt").write_text("keep", encoding="utf-8")
        link = self.case / "linked-root"
        self.make_link(link, real)
        self.call("plan", "--path", "keep.txt", root=link, success=False)
        self.assertEqual((real / "keep.txt").read_text(), "keep")

    def test_junction_root_ancestor_refused(self):
        real = self.case / "real"
        (real / "child").mkdir(parents=True)
        (real / "child/keep.txt").write_text("keep", encoding="utf-8")
        link = self.case / "ancestor"
        self.make_link(link, real)
        self.call("plan", "--path", "keep.txt", root=link / "child", success=False)
        self.assertTrue((real / "child/keep.txt").exists())

    def test_junction_selected_item_and_parent_refused(self):
        outside = self.case / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        self.make_link(self.root / "linked", outside)
        self.plan("linked", success=False)
        self.plan("linked/keep.txt", success=False)
        self.assertEqual((outside / "keep.txt").read_text(), "keep")

    def test_junction_descendant_refuses_whole_directory(self):
        self.write("draft/local.txt")
        outside = self.case / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        self.make_link(self.root / "draft/linked", outside)
        self.plan("draft", success=False)
        self.assertTrue((self.root / "draft/local.txt").exists())
        self.assertTrue((outside / "keep.txt").exists())

    def test_file_symlink_is_not_followed(self):
        outside = self.case / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        self.make_link(self.root / "link.txt", outside, directory=False)
        self.plan("link.txt", success=False)
        self.assertEqual(outside.read_text(), "keep")

    def test_root_replacement_invalidates_plan(self):
        self.write("one.txt")
        plan = self.plan("one.txt")
        old = self.case / "old-root"
        self.assertTrue(old.absolute().is_relative_to(self.case.absolute()))
        self.root.rename(old)
        self.root.mkdir()
        self.write("one.txt", "different root")
        self.apply(plan, success=False)
        self.assertEqual((old / "one.txt").read_text(), "fixture")
        self.assertEqual((self.root / "one.txt").read_text(), "different root")

    def test_existing_unowned_quarantine_refused(self):
        self.write("one.txt")
        self.write(".project-trash/keep.txt", "existing")
        self.apply(self.plan("one.txt"), success=False)
        self.assertEqual((self.root / ".project-trash/keep.txt").read_text(), "existing")
        self.assertTrue((self.root / "one.txt").exists())

    def test_quarantine_junction_refused_before_move(self):
        self.write("one.txt")
        outside = self.case / "outside"
        outside.mkdir()
        self.make_link(self.root / ".project-trash", outside)
        self.apply(self.plan("one.txt"), success=False)
        self.assertTrue((self.root / "one.txt").exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_manifest_path_tampering_cannot_restore_outside_root(self):
        self.write("one.txt")
        result = self.apply(self.plan("one.txt"))
        path = self.batch(result) / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["plan"]["targets"][0]["path"] = "../escape.txt"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.call("restore", "--batch", result["batch"], success=False)
        self.call("purge", "--batch", result["batch"], "--permanent", success=False)
        self.assertFalse((self.case / "escape.txt").exists())
        self.assertTrue((self.batch(result) / "items/0000").exists())

    def test_unknown_batch_sibling_refuses_restore_and_purge(self):
        self.write("one.txt")
        result = self.apply(self.plan("one.txt"))
        extra = self.batch(result) / "extra.txt"
        extra.write_text("unknown", encoding="utf-8")
        self.call("restore", "--batch", result["batch"], success=False)
        self.call("purge", "--batch", result["batch"], "--permanent", success=False)
        self.assertTrue(extra.exists())
        self.assertTrue((self.batch(result) / "items/0000").exists())

    def test_missing_original_parent_blocks_entire_restore(self):
        self.write("one.txt")
        self.write("parent/two.txt")
        result = self.apply(self.plan("one.txt", "parent/two.txt"))
        parent = self.root / "parent"
        self.assertTrue(parent.resolve().is_relative_to(self.root.resolve()))
        parent.rmdir()
        self.call("restore", "--batch", result["batch"], success=False)
        self.assertFalse((self.root / "one.txt").exists())
        self.assertTrue((self.batch(result) / "items/0000").exists())
        self.assertTrue((self.batch(result) / "items/0001").exists())

    def test_restore_parent_junction_is_refused(self):
        self.write("parent/one.txt")
        result = self.apply(self.plan("parent/one.txt"))
        parent = self.root / "parent"
        self.assertTrue(parent.resolve().is_relative_to(self.root.resolve()))
        parent.rmdir()
        outside = self.case / "outside"
        outside.mkdir()
        self.make_link(parent, outside)
        self.call("restore", "--batch", result["batch"], success=False)
        self.assertEqual(list(outside.iterdir()), [])

    def test_batch_id_does_not_accept_traversal(self):
        self.call("restore", "--batch", "../outside", success=False)
        self.call("purge", "--batch", "../outside", "--permanent", success=False)

    def test_empty_directory_can_be_restored_and_batch_purged(self):
        (self.root / "empty").mkdir()
        first = self.apply(self.plan("empty"))
        self.call("restore", "--batch", first["batch"])
        self.assertTrue((self.root / "empty").is_dir())
        second = self.apply(self.plan("empty"))
        self.call("purge", "--batch", second["batch"], "--permanent")
        self.assertFalse((self.root / "empty").exists())

    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics")
    def test_real_file_lock_after_first_move_retains_partial_journal(self):
        import ctypes
        from ctypes import wintypes
        self.write("one.txt", "one")
        second = self.write("two.txt", "two")
        plan = self.plan("one.txt", "two.txt")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        handle = create(str(second), 0x80000000, 1, None, 3, 0x80, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value, ctypes.get_last_error())
        try:
            result = self.apply(plan, success=False)
        finally:
            close(handle)
        self.assertEqual(result["phase"], "apply")
        self.assertFalse((self.root / "one.txt").exists())
        self.assertEqual(second.read_text(), "two")
        self.assertEqual((self.batch(result) / "items/0000").read_text(), "one")
        manifest = json.loads((self.batch(result) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "apply_failed")
        self.assertEqual(manifest["progress"], ["stored", "moving"])
        self.call("restore", "--batch", result["batch"], success=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
