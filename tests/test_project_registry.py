"""Incremental project records must not overwrite unrelated or edited rules."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "skills/project-directory-organizer/scripts/project_rules.py"


def project(identifier="alpha", **changes):
    record = {"id": identifier, "path": identifier, "purpose": "Documentation", "status": "planned",
              "language": "english", "environment": None, "outputs": [identifier + "/reports"],
              "rules": ["Keep original filenames."]}
    record.update(changes)
    return record


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="registry-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.case = Path(self.temporary.name)
        self.root = self.case / "root"
        self.root.mkdir()
        self.rules = self.root / "PROJECT_RULES.md"
        self.original = b"\xef\xbb\xbf# Shared rules\r\nKeep this hand-written paragraph.\r\n"
        self.rules.write_bytes(self.original)
        (self.root / "AGENTS.md").write_bytes(b"Keep custom entry.\r\n")

    def call(self, command, request, success=True, root=None):
        args = [sys.executable, "-B", "-X", "utf8", str(SCRIPT), command, str(root or self.root)]
        args += ["--projects-file" if command == "update-plan" else "--plan-file", "-"]
        if command == "apply":
            args += ["--confirmed-plan-digest", request["plan_digest"]]
        result = subprocess.run(args, input=json.dumps(request), encoding="utf-8", capture_output=True)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        self.assertTrue(result.stdout.startswith("{"), result.stderr)
        return json.loads(result.stdout)

    def plan(self, records=None, **options):
        return self.call("update-plan", {"projects": records or [project()], **options})

    def initialize(self, records=None):
        plan = self.plan(records, initialize_after=self.original.decode("utf-8"))
        self.assertTrue(plan["ready_to_apply"], plan)
        self.call("apply", plan)
        return plan

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def test_explicit_initialization_is_read_only_and_does_not_create_business_paths(self):
        before = self.snapshot()
        plan = self.plan(initialize_after=self.original.decode("utf-8"))
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(plan["ready_to_apply"])
        self.assertIn("+", plan["diff"])
        self.call("apply", plan)
        self.assertTrue(self.rules.read_bytes().startswith(self.original))
        self.assertEqual(set(self.snapshot()), {"PROJECT_RULES.md", "AGENTS.md"})
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), before["AGENTS.md"])

    def test_update_preserves_other_project_and_surrounding_bytes(self):
        self.initialize([project(), project("beta")])
        suffix = b"\r\n## Hand-written appendix\r\nDo not normalize this.\n"
        self.rules.write_bytes(self.rules.read_bytes() + suffix)
        before = self.rules.read_bytes()
        beta = before[before.index(b"<!-- project-directory-organizer:project:start id=beta"):]
        plan = self.plan([project(purpose="Updated purpose", status="archived")])
        self.call("apply", plan)
        result = self.rules.read_bytes()
        self.assertTrue(result.startswith(self.original))
        self.assertTrue(result.endswith(beta))
        self.assertIn(b"Updated purpose", result)
        self.assertEqual(result.count(b"project:start id=alpha "), 1)

    def test_repeat_plan_is_noop_and_old_plan_is_stale(self):
        old = self.initialize()
        before = self.snapshot()
        repeated = self.plan()
        self.assertEqual(repeated["diff"], "")
        self.assertEqual(repeated["files"][0]["action"], "keep")
        self.call("apply", repeated)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.call("apply", old, success=False)["code"], "plan_changed")

    def test_manually_edited_generated_body_blocks_even_another_project_update(self):
        self.initialize([project(), project("beta")])
        self.rules.write_bytes(self.rules.read_bytes().replace(b"Documentation", b"Hand edited", 1))
        before = self.snapshot()
        plan = self.plan([project("beta", purpose="Beta change")])
        self.assertFalse(plan["ready_to_apply"])
        self.assertIn("managed_content_changed", {item["code"] for item in plan["blockers"]})
        self.call("apply", plan, success=False)
        self.assertEqual(self.snapshot(), before)

    def test_legacy_document_requires_exact_migration(self):
        old = "## Legacy alpha\r\nalpha purpose and custom text\r\n"
        self.rules.write_bytes(self.original + old.encode() + b"\r\nKeep appendix.\r\n")
        self.assertFalse(self.plan()["ready_to_apply"])
        plan = self.plan([project(before=old)])
        self.assertTrue(plan["ready_to_apply"], plan)
        self.call("apply", plan)
        raw = self.rules.read_bytes()
        self.assertTrue(raw.startswith(self.original))
        self.assertTrue(raw.endswith(b"\r\nKeep appendix.\r\n"))
        self.assertNotIn(b"Legacy alpha", raw)

    def test_ambiguous_or_partial_migration_and_initialization_are_blocked(self):
        variants = [("One paragraph.\nOne paragraph.\n", {"projects": [project(before="One paragraph.\n")]}),
                    ("Prefix middle suffix\n", {"projects": [project(before="middle")]}),
                    ("# Project registry\n| alpha | alpha |\n", {"projects": [project()], "initialize_after": "# Project registry\n"}),
                    ("Prefix middle suffix\n", {"projects": [project()], "initialize_after": "middle"})]
        for text, request in variants:
            with self.subTest(text=text, request=request):
                self.rules.write_text(text, encoding="utf-8")
                self.assertFalse(self.call("update-plan", request)["ready_to_apply"])

    def test_duplicate_malformed_and_fenced_markers_block(self):
        self.initialize()
        valid = self.rules.read_bytes()[len(self.original):]
        for data in (self.original + valid + valid,
                     self.original + valid.replace(b"sha256=", b"hash="),
                     self.original + b"```md\r\n" + valid + b"```\r\n",
                     self.original + b"~~~\r\n" + valid + b"~~~\r\n",
                     self.original + b"    " + valid.lstrip()):
            with self.subTest(data=data):
                self.rules.write_bytes(data)
                self.assertFalse(self.plan()["ready_to_apply"])

    def test_unclosed_fence_and_migration_inside_fence_block(self):
        for text, options in (("# Rules\n```\n", {"initialize_after": "# Rules\n"}),
                              ("```\nlegacy section\n```\n", {})):
            self.rules.write_text(text, encoding="utf-8")
            records = [project(before="legacy section\n")] if not options else [project()]
            self.assertFalse(self.plan(records, **options)["ready_to_apply"])

    def test_input_rejects_unsafe_paths_marker_injection_and_wrong_ownership(self):
        variants = [project(path="../escape"), project(path="C:/escape"), project(path="a/NUL"),
                    project(environment="beta/.venv"), project(outputs=["beta/reports"]),
                    project(environment="alpha"), project(outputs=["alpha"]),
                    project(id="bad -->"), project(purpose="<!-- project-directory-organizer:project:start -->"),
                    project(rules=["first\nsecond"]), project(status="deleted"),
                    project(language="auto"), project(outputs=["alpha/out", "ALPHA/out"])]
        before = self.snapshot()
        for record in variants:
            with self.subTest(record=record):
                self.call("update-plan", {"projects": [record]}, success=False)
        self.assertEqual(self.snapshot(), before)

    def test_root_overlap_and_existing_project_ownership_conflicts_block(self):
        self.initialize()
        for record in (project("beta", path="alpha", outputs=[]), project("beta", path="alpha/child", outputs=[])):
            with self.subTest(record=record):
                self.assertFalse(self.plan([record])["ready_to_apply"])
        self.call("update-plan", {"projects": [project(), project("beta", path="alpha/child", outputs=[])]}, success=False)

    def test_file_and_root_changes_and_forged_plan_are_rejected(self):
        self.initialize()
        plan = self.plan([project(purpose="Updated")])
        before = self.rules.read_bytes()
        self.rules.write_bytes(before + b"Concurrent edit\r\n")
        self.assertEqual(self.call("apply", plan, success=False)["code"], "plan_changed")
        self.rules.write_bytes(before)
        plan = self.plan([project(purpose="Updated")])
        forged = copy.deepcopy(plan)
        forged["files"][0]["content"] = "forged replacement"
        payload = {key: value for key, value in forged.items() if key != "plan_digest"}
        forged["plan_digest"] = hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(self.call("apply", forged, success=False)["code"], "plan_changed")
        moved = self.case / "moved-root"
        self.root.rename(moved)
        self.root.mkdir()
        self.rules.write_bytes(before)
        self.assertEqual(self.call("apply", plan, success=False)["code"], "plan_changed")

    def test_symlink_in_selected_project_path_is_rejected(self):
        outside = self.case / "outside"
        outside.mkdir()
        if os.name == "nt":
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(self.root / "alpha"), str(outside)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.call("update-plan", {"projects": [project()]}, success=False)
            self.assertEqual(list(outside.iterdir()), [])
            return
        try:
            (self.root / "alpha").symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest("Symlink creation unavailable: " + str(error))
        self.call("update-plan", {"projects": [project()]}, success=False)

    def test_marker_metadata_must_agree_with_generated_body(self):
        self.initialize()
        initial = self.rules.read_bytes()
        for content in (initial.replace(b"id=alpha", b"id=beta"),
                        initial.replace(b"path=alpha sha256", b"path=beta sha256")):
            self.rules.write_bytes(content)
            self.assertFalse(self.plan([project("beta")])["ready_to_apply"])

    def test_bom_immediately_before_first_managed_marker_is_preserved(self):
        self.initialize()
        self.rules.write_bytes(b"\xef\xbb\xbf" + self.rules.read_bytes()[len(self.original):].lstrip())
        plan = self.plan([project(purpose="Updated")])
        self.assertTrue(plan["ready_to_apply"], plan)
        self.call("apply", plan)
        self.assertTrue(self.rules.read_bytes().startswith(b"\xef\xbb\xbf<!--"))

    def test_json_duplicate_keys_are_rejected_before_planning(self):
        payload = json.dumps({"projects": [project()]})
        payload = payload.replace('"purpose": "Documentation"', '"purpose": "First", "purpose": "Documentation"')
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "update-plan", str(self.root), "--projects-file", "-"],
                                input=payload, encoding="utf-8", capture_output=True)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.rules.read_bytes(), self.original)

    def test_environment_and_outputs_do_not_overlap(self):
        for environment, output in (("alpha/.venv", "alpha/.venv/reports"), ("alpha/work/.venv", "alpha/work")):
            self.call("update-plan", {"projects": [project(environment=environment, outputs=[output])]}, success=False)

    def test_missing_base_document_and_hardlinks_are_rejected(self):
        self.rules.unlink()
        self.call("update-plan", {"projects": [project()]}, success=False)
        outside = self.case / "external-rules"
        outside.write_bytes(self.original)
        os.link(outside, self.rules)
        self.call("update-plan", {"projects": [project()]}, success=False)
        self.assertEqual(outside.read_bytes(), self.original)

    def test_locked_rule_file_refuses_write(self):
        self.initialize()
        plan = self.plan([project(purpose="Changed")])
        before = self.rules.read_bytes()
        with self.rules.open("r+b") as stream:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, len(before))
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                self.call("apply", plan, success=False)
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, len(before))
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        self.assertEqual(self.rules.read_bytes(), before)

    def test_new_project_keeps_existing_unmentioned_registry_and_uses_own_rules(self):
        self.initialize()
        before = self.rules.read_bytes()
        plan = self.plan([project("beta", language="chinese", environment="beta/环境", outputs=["beta/报告"],
                                  rules=["独立保存原件 <raw>；不继承相邻项目规则。"])])
        self.assertTrue(plan["ready_to_apply"], plan)
        self.call("apply", plan)
        after = self.rules.read_bytes()
        self.assertTrue(after.startswith(before))
        self.assertIn("beta/环境".encode(), after)
        self.assertIn(b"&lt;raw&gt;", after)
        self.assertEqual(after.count(b"project:start id=alpha "), 1)
        self.assertEqual(after.count(b"project:start id=beta "), 1)

    def test_unmanaged_descendant_path_references_block_initialization(self):
        for path, reference in (("alpha", "alpha/reports"), ("alpha", "alpha/.venv"),
                                ("alpha", r"alpha\reports"), ("work/alpha", "work/alpha/reports"),
                                ("work/alpha", r"work\alpha\reports")):
            with self.subTest(path=path, reference=reference):
                original = ("# Rules\n## Existing work\n- Existing location: " + reference + "\n").encode()
                self.rules.write_bytes(original)
                record = project("record", path=path, environment=None, outputs=[])
                plan = self.plan([record], initialize_after="# Rules\n")
                self.assertFalse(plan["ready_to_apply"])
                self.assertEqual(plan["diff"], "")
                self.call("apply", plan, success=False)
                self.assertEqual(self.rules.read_bytes(), original)

    def test_other_root_names_are_not_mistaken_for_project_descendants(self):
        for reference in ("alphabet/reports", "alpha-other/reports", "other/alpha/reports", r"other\alpha\reports"):
            with self.subTest(reference=reference):
                self.rules.write_bytes(("# Rules\n- Existing location: " + reference + "\n").encode())
                plan = self.plan([project(outputs=[])], initialize_after="# Rules\n")
                self.assertTrue(plan["ready_to_apply"], plan["blockers"])

    def test_changed_marker_line_ending_blocks_without_normalizing_body(self):
        for newline, changed in ((b"\r\n", b"\n"), (b"\n", b"\r\n")):
            for marker in (b":project:start", b":project:end"):
                with self.subTest(newline=newline, marker=marker):
                    self.rules.write_bytes(b"# Rules" + newline)
                    plan = self.plan(initialize_after=(b"# Rules" + newline).decode())
                    self.assertTrue(plan["ready_to_apply"], plan["blockers"])
                    self.call("apply", plan)
                    # A valid LF or CRLF block must remain a no-op before mutation.
                    self.assertEqual(self.plan()["files"][0]["action"], "keep")
                    original = self.rules.read_bytes()
                    lines = original.splitlines(keepends=True)
                    index = next(index for index, line in enumerate(lines) if marker in line)
                    lines[index] = lines[index][:-len(newline)] + changed
                    modified = b"".join(lines)
                    self.rules.write_bytes(modified)
                    plan = self.plan()
                    self.assertFalse(plan["ready_to_apply"])
                    self.assertEqual(plan["diff"], "")
                    self.call("apply", plan, success=False)
                    self.assertEqual(self.rules.read_bytes(), modified)


if __name__ == "__main__":
    unittest.main(verbosity=2)
