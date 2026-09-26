"""Rules planning preserves user files and binds writes to the reviewed plan."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

TASK = Path(__file__).resolve().parents[1]
SOURCE = TASK / "skills/project-directory-organizer"
TEMPLATES = {
    "AGENTS.md": "# {{PROJECT_NAME}}\n{{TIMEZONE}} / {{LANGUAGE}}\n<!-- project-directory-organizer:rules:start -->\nRead [rules]({{RULES_FILE}}). 原件保持原样。\n<!-- project-directory-organizer:rules:end -->\n",
    "PROJECT_RULES.md": "# {{PROJECT_NAME}} rules\n{{TIMEZONE}} / {{LANGUAGE}}\n{{PROJECT_SCOPE}}\n{{ENVIRONMENT_RULE}}\n{{DIRECTORY_TABLE}}\n{{OUTPUT_RULES}}\n{{ORIGINAL_RULES}}\n",
}
LAYOUT = {
    "research": "Breast cancer literature review",
    "directories": [
        {"path": "breast-cancer/work", "purpose": "Working notes"},
        {"path": "breast-cancer/reference", "purpose": "Original papers"},
        {"path": "breast-cancer/reports", "purpose": "Reports and slide decks"},
    ],
    "environment": None,
    "outputs": ["breast-cancer/reports"],
    "originals": ["breast-cancer/reference"],
}


class RulesTests(unittest.TestCase):
    def setUp(self):
        runs = Path(tempfile.gettempdir()) / "project-directory-organizer-tests"
        runs.mkdir(exist_ok=True)
        self.case = Path(tempfile.mkdtemp(prefix="rules-", dir=runs))
        self.root = self.case / "癌症"
        self.root.mkdir()
        self.bundle = self.case / "bundle"
        (self.bundle / "scripts").mkdir(parents=True)
        (self.bundle / "assets/templates").mkdir(parents=True)
        for name in ("project_rules.py", "path_safety.py"):
            source = SOURCE / "scripts" / name
            if source.exists():
                shutil.copyfile(source, self.bundle / "scripts" / name)
        for name, content in TEMPLATES.items():
            (self.bundle / "assets/templates" / name).write_text(content, encoding="utf-8")
        self.script = self.bundle / "scripts/project_rules.py"

    def call(self, *args, success=True, layout=LAYOUT, payload=None):
        args = list(args)
        if args and args[0] == "plan" and layout is not None and "--layout-file" not in args:
            path = self.case / "selected-layout.json"
            path.write_text(json.dumps(layout, ensure_ascii=True), encoding="utf-8")
            args.extend(("--layout-file", path))
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", str(self.script), *map(str, args)], input=payload, capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def plan(self, *args):
        return self.call("plan", self.root, "--language", "chinese", *args)

    def save(self, plan):
        path = self.case / "reviewed-plan.json"
        path.write_text(json.dumps(plan, ensure_ascii=True), encoding="utf-8")
        return path

    def apply(self, plan, success=True, digest=None):
        return self.call("apply", self.root, "--plan-file", self.save(plan), "--confirmed-plan-digest", digest or plan["plan_digest"], success=success)

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def test_plan_is_read_only_and_apply_creates_only_two_rule_files(self):
        plan = self.plan()
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual({item["action"] for item in plan["files"]}, {"create"})
        self.assertEqual(plan["blockers"], [])
        self.apply(plan)
        self.assertEqual(set(self.snapshot()), {"AGENTS.md", "PROJECT_RULES.md"})
        self.assertEqual({p.name for p in self.root.iterdir()}, {"AGENTS.md", "PROJECT_RULES.md"})
        self.assertIn("癌症", (self.root / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertNotIn("{{", (self.root / "AGENTS.md").read_text(encoding="utf-8"))

    def test_plan_without_bytecode_flag_does_not_create_bundle_cache(self):
        environment = dict(os.environ)
        environment.pop("PYTHONDONTWRITEBYTECODE", None)
        result = subprocess.run([sys.executable, "-X", "utf8", str(self.script), "plan", str(self.root), "--language", "chinese", "--layout-file", "-"], input=json.dumps(LAYOUT), capture_output=True, encoding="utf-8", env=environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(list(self.bundle.rglob("__pycache__")), [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_nonempty_project_is_preserved_and_existing_agents_gets_append_only(self):
        original = b"\xef\xbb\xbf# User rules\r\nKeep these instructions.\r\n"
        (self.root / "AGENTS.md").write_bytes(original)
        (self.root / "notes.txt").write_bytes(b"keep")
        plan = self.plan()
        item = next(item for item in plan["files"] if item["path"] == "AGENTS.md")
        self.assertEqual(item["action"], "append")
        self.assertEqual(item["before"]["sha256"], hashlib.sha256(original).hexdigest())
        self.assertIn("PROJECT_RULES.md", item["content"])
        self.assertIn("原件保持原样", item["content"])
        self.apply(plan)
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), original + item["content"].encode("utf-8"))
        self.assertEqual((self.root / "notes.txt").read_bytes(), b"keep")

    def test_repeat_is_idempotent(self):
        (self.root / "AGENTS.md").write_text("User instructions", encoding="utf-8")
        self.apply(self.plan())
        before = self.snapshot()
        plan = self.plan()
        self.assertEqual({item["action"] for item in plan["files"]}, {"keep"})
        self.apply(plan)
        self.assertEqual(self.snapshot(), before)

    def test_template_managed_paragraph_survives_unrelated_agents_additions(self):
        self.apply(self.plan())
        agents = self.root / "AGENTS.md"
        agents.write_bytes(agents.read_bytes() + b"\nAdditional user instructions\n")
        before = self.snapshot()
        plan = self.plan()
        self.assertEqual(plan["blockers"], [])
        self.assertEqual({item["action"] for item in plan["files"]}, {"keep"})
        self.apply(plan)
        self.assertEqual(self.snapshot(), before)

    def test_existing_custom_rules_are_kept_without_layout_or_manual_merge(self):
        original = b"\xef\xbb\xbf# User's own existing rules\r\nCustom data policy.\r\n"
        (self.root / "PROJECT_RULES.md").write_bytes(original)
        plan = self.call("plan", self.root, "--language", "chinese", layout=None)
        self.assertEqual(plan["blockers"], [])
        self.assertTrue(plan["ready_to_apply"])
        self.assertEqual(next(item for item in plan["files"] if item["path"] == "PROJECT_RULES.md")["action"], "keep")
        self.apply(plan)
        self.assertEqual((self.root / "PROJECT_RULES.md").read_bytes(), original)
        repeated = self.call("plan", self.root, "--language", "chinese", layout=None)
        self.assertEqual({item["action"] for item in repeated["files"]}, {"keep"})

    def test_new_rules_require_selected_layout(self):
        result = self.call("plan", self.root, "--language", "chinese", layout=None, success=False)
        self.assertEqual(result["code"], "layout_required")
        self.assertEqual(self.snapshot(), {})

    def test_only_selected_structure_is_rendered_without_environment_or_candidates(self):
        plan = self.plan()
        self.assertEqual(plan["layout"], LAYOUT)
        self.apply(plan)
        text = (self.root / "PROJECT_RULES.md").read_text(encoding="utf-8")
        for item in LAYOUT["directories"]:
            self.assertIn(item["path"], text)
            self.assertIn(item["purpose"], text)
        self.assertIn(LAYOUT["research"], text)
        self.assertIn("当前未设置项目环境", text)
        self.assertNotIn("env/", text)
        self.assertNotIn("data/raw", text)
        self.assertEqual({p.name for p in self.root.iterdir()}, {"AGENTS.md", "PROJECT_RULES.md"})

    def test_chinese_layout_and_environment_are_rendered_with_safe_markdown(self):
        layout = {"research": "胃癌 [初期] 文献梳理", "directories": [{"path": "胃癌/报告", "purpose": "PDF | PPT 汇报"}, {"path": "环境", "purpose": "研究环境预留"}], "environment": "环境", "outputs": ["胃癌/报告"], "originals": []}
        plan = self.call("plan", self.root, "--language", "chinese", layout=layout)
        self.apply(plan)
        text = (self.root / "PROJECT_RULES.md").read_text(encoding="utf-8")
        self.assertIn("胃癌/报告", text)
        self.assertIn("PDF \\| PPT", text)
        self.assertIn("胃癌 \\[初期\\] 文献梳理", text)
        self.assertIn("目录预留不代表已安装", text)
        self.assertNotIn(str(self.root), text)

    def test_no_outputs_omits_archiving_section(self):
        layout = dict(LAYOUT, outputs=[])
        plan = self.call("plan", self.root, "--language", "english", layout=layout)
        self.apply(plan)
        self.assertNotIn("## 成果归档", (self.root / "PROJECT_RULES.md").read_text(encoding="utf-8"))

    def test_invalid_layouts_are_rejected_without_writing(self):
        invalid = []
        for path in ("../outside", "C:/outside", "/absolute", "reports/../outside", "reports/CON.txt", "reports/name.", "reports/name ", "reports/file:stream", "reports/*", "reports//batch"):
            invalid.append(dict(LAYOUT, directories=[{"path": path, "purpose": "Selected"}], outputs=[], originals=[]))
        invalid.extend([
            dict(LAYOUT, directories=[{"path": "Reports", "purpose": "a"}, {"path": "reports", "purpose": "b"}], outputs=[], originals=[]),
            dict(LAYOUT, environment="unselected-env"),
            dict(LAYOUT, outputs=["unselected-reports"]),
            dict(LAYOUT, originals=["../outside"]),
            dict(LAYOUT, research="scope\nnew instruction"),
            dict(LAYOUT, directories=[{"path": "safe", "purpose": "text\nnew instruction"}], outputs=[], originals=[]),
            dict(LAYOUT, directories="not-a-list"),
            dict(LAYOUT, environment=True),
        ])
        for layout in invalid:
            with self.subTest(layout=layout):
                result = self.call("plan", self.root, "--language", "chinese", layout=layout, success=False)
                self.assertEqual(result["code"], "invalid_layout")
                self.assertEqual(self.snapshot(), {})

    def test_layout_is_bound_to_the_displayed_plan(self):
        plan = self.plan()
        plan["layout"]["directories"][0]["purpose"] = "Unreviewed purpose"
        self.apply(plan, success=False)
        payload = {key: value for key, value in plan.items() if key != "plan_digest"}
        plan["plan_digest"] = hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        self.apply(plan, success=False)
        self.assertEqual(self.snapshot(), {})

    def test_layout_can_be_read_from_stdin_without_project_cache(self):
        plan = self.call("plan", self.root, "--language", "chinese", "--layout-file", "-", payload=json.dumps(LAYOUT))
        self.assertEqual(plan["layout"], LAYOUT)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_originals_can_be_selected_directory_descendants(self):
        layout = dict(LAYOUT, directories=[{"path": "gastric-cancer/data", "purpose": "Selected data"}], outputs=[], originals=["gastric-cancer/data/raw"])
        plan = self.call("plan", self.root, "--language", "english", layout=layout)
        self.apply(plan)
        self.assertIn("gastric-cancer/data/raw", (self.root / "PROJECT_RULES.md").read_text(encoding="utf-8"))
        self.assertFalse((self.root / "gastric-cancer").exists())

    def test_original_and_output_locations_must_not_overlap(self):
        for original, output in (("data", "data"), ("data", "data/results"), ("data/raw", "data")):
            layout = {"research": "Selected research", "directories": [{"path": "data", "purpose": "Data"}, {"path": "data/results", "purpose": "Results"}], "environment": None, "outputs": [output], "originals": [original]}
            with self.subTest(original=original, output=output):
                result = self.call("plan", self.root, "--language", "english", layout=layout, success=False)
                self.assertEqual(result["code"], "invalid_layout")
                self.assertEqual(self.snapshot(), {})

    def test_output_rules_preserve_batch_and_version_requirements_in_selected_language(self):
        for language, batch, filename in (("english", "YYYYMMDD-purpose", "content-name_v01.ext"), ("chinese", "YYYYMMDD用途", "内容名_v01.ext")):
            plan = self.call("plan", self.root, "--language", language)
            text = next(item["content"] for item in plan["files"] if item["path"] == "PROJECT_RULES.md")
            for phrase in (batch, filename, "实际产出或明确预留", "不强制成对", "同日同次", "不同用途", "跨日连续", "不覆盖旧版本", "PPT/PDF", "不复制未变化", "不随日期移动"):
                self.assertIn(phrase, text)

    def test_custom_rules_encoding_and_unrelated_template_changes_do_not_trigger_merge(self):
        custom = "自定义规则，保留原始编码".encode("utf-16")
        (self.root / "PROJECT_RULES.md").write_bytes(custom)
        plan = self.call("plan", self.root, "--language", "chinese", layout=None)
        (self.bundle / "assets/templates/PROJECT_RULES.md").write_text("unrelated changed template {{UNKNOWN}}", encoding="utf-8")
        self.apply(plan)
        self.assertEqual((self.root / "PROJECT_RULES.md").read_bytes(), custom)

    def test_existing_selected_path_junction_is_rejected(self):
        if os.name != "nt":
            self.skipTest("Windows junction test")
        outside = self.case / "outside-layout"
        outside.mkdir()
        linked = self.root / "linked"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(linked), str(outside)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        layout = dict(LAYOUT, directories=[{"path": "linked/reports", "purpose": "Outside"}], outputs=[], originals=[])
        result = self.call("plan", self.root, "--language", "chinese", layout=layout, success=False)
        self.assertEqual(result["code"], "invalid_layout")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.root / "PROJECT_RULES.md").exists())

    def test_existing_selected_path_file_is_not_a_directory(self):
        (self.root / "breast-cancer").write_bytes(b"a file")
        result = self.call("plan", self.root, "--language", "chinese", success=False)
        self.assertEqual(result["code"], "invalid_layout")
        self.assertEqual((self.root / "breast-cancer").read_bytes(), b"a file")

    def test_duplicate_layout_json_keys_are_rejected(self):
        source = '{"research":"first","research":"second","directories":[],"environment":null,"outputs":[],"originals":[]}'
        result = self.call("plan", self.root, "--language", "chinese", "--layout-file", "-", payload=source, success=False)
        self.assertEqual(result["code"], "invalid_layout")
        self.assertEqual(self.snapshot(), {})

    def test_existing_rule_link_without_managed_confirmation_requires_manual_merge(self):
        (self.root / "AGENTS.md").write_text("See [local guide](./PROJECT_RULES.md).", encoding="utf-8")
        plan = self.plan()
        self.assertTrue(any(item["path"] == "AGENTS.md" for item in plan["blockers"]))
        self.apply(plan, success=False)
        self.assertFalse((self.root / "PROJECT_RULES.md").exists())

    def test_unclosed_markdown_fence_requires_manual_merge_without_changes(self):
        for index, fence in enumerate(("```text", "~~~~python", "   `````markdown")):
            target = self.case / ("unclosed-fence-" + str(index))
            target.mkdir()
            agents = target / "AGENTS.md"
            original = ("# Existing user rules\r\n\r\n" + fence + "\r\nsample content\r\n").encode("utf-8")
            agents.write_bytes(original)
            plan = self.call("plan", target, "--language", "chinese")
            self.assertTrue(any(item["path"] == "AGENTS.md" and item["code"] == "manual_merge_required" for item in plan["blockers"]))
            self.call("apply", target, "--plan-file", self.save(plan), "--confirmed-plan-digest", plan["plan_digest"], success=False)
            self.assertEqual(agents.read_bytes(), original)
            self.assertEqual({item.name for item in target.iterdir()}, {"AGENTS.md"})

    def test_matching_managed_block_inside_closed_fence_is_not_effective_rules(self):
        for index, fence in enumerate(("```", "~~~~")):
            target = self.case / ("quoted-block-" + str(index))
            target.mkdir()
            initial = self.call("plan", target, "--language", "chinese")
            self.call("apply", target, "--plan-file", self.save(initial), "--confirmed-plan-digest", initial["plan_digest"])
            agents = target / "AGENTS.md"
            original = (fence + "markdown\n" + agents.read_text(encoding="utf-8") + fence + "\n").encode("utf-8")
            agents.write_bytes(original)
            rules_before = (target / "PROJECT_RULES.md").read_bytes()
            plan = self.call("plan", target, "--language", "chinese")
            self.assertTrue(any(item["path"] == "AGENTS.md" and item["code"] == "manual_merge_required" for item in plan["blockers"]))
            self.call("apply", target, "--plan-file", self.save(plan), "--confirmed-plan-digest", plan["plan_digest"], success=False)
            self.assertEqual(agents.read_bytes(), original)
            self.assertEqual((target / "PROJECT_RULES.md").read_bytes(), rules_before)

    def test_closed_unrelated_fence_does_not_prevent_safe_append(self):
        for index, content in enumerate(("```text\nexample\n```\n", "~~~~\n```\n~~~~~\n")):
            target = self.case / ("closed-example-" + str(index))
            target.mkdir()
            agents = target / "AGENTS.md"
            agents.write_text(content, encoding="utf-8")
            plan = self.call("plan", target, "--language", "chinese")
            self.assertEqual(plan["blockers"], [])
            self.call("apply", target, "--plan-file", self.save(plan), "--confirmed-plan-digest", plan["plan_digest"])
            self.assertTrue(agents.read_text(encoding="utf-8").startswith(content))

    def test_changed_existing_agents_rejects_entire_plan_before_any_write(self):
        (self.root / "AGENTS.md").write_text("first", encoding="utf-8")
        plan = self.plan()
        (self.root / "AGENTS.md").write_text("changed", encoding="utf-8")
        before = self.snapshot()
        self.apply(plan, success=False)
        self.assertEqual(self.snapshot(), before)

    def test_new_destination_collision_rejects_entire_plan(self):
        plan = self.plan()
        (self.root / "AGENTS.md").write_text("concurrent content", encoding="utf-8")
        before = self.snapshot()
        self.apply(plan, success=False)
        self.assertEqual(self.snapshot(), before)

    def test_confirmation_digest_must_match(self):
        plan = self.plan()
        self.apply(plan, success=False, digest="0" * 64)
        self.assertEqual(self.snapshot(), {})

    def test_tampering_with_plan_is_rejected(self):
        plan = self.plan()
        plan["files"][0]["content"] = "unreviewed replacement"
        self.apply(plan, success=False)
        self.assertEqual(self.snapshot(), {})

    def test_template_change_after_plan_rejected(self):
        plan = self.plan()
        (self.bundle / "assets/templates/PROJECT_RULES.md").write_text("changed template", encoding="utf-8")
        self.apply(plan, success=False)
        self.assertEqual(self.snapshot(), {})

    def test_rehashed_forged_plan_is_rejected_against_regenerated_plan(self):
        plan = self.plan()
        plan["files"][0]["content"] = "unreviewed replacement"
        payload = {key: value for key, value in plan.items() if key != "plan_digest"}
        plan["plan_digest"] = hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        self.apply(plan, success=False)
        self.assertEqual(self.snapshot(), {})

    def test_apply_accepts_stdin_without_storing_plan_in_project(self):
        plan = self.plan()
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", str(self.script), "apply", str(self.root), "--plan-file", "-", "--confirmed-plan-digest", plan["plan_digest"]], input=json.dumps(plan, ensure_ascii=True), capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(set(self.snapshot()), {"AGENTS.md", "PROJECT_RULES.md"})

    def test_replaced_file_with_same_bytes_is_rejected(self):
        target = self.root / "AGENTS.md"
        target.write_bytes(b"User rules")
        plan = self.plan()
        target.rename(self.root / "original-agents.txt")
        target.write_bytes(b"User rules")
        before = self.snapshot()
        self.apply(plan, success=False)
        self.assertEqual(self.snapshot(), before)

    def test_actual_bundle_templates_render_both_languages_and_repeat(self):
        for language in ("chinese", "english"):
            target = self.case / (language + "-project")
            target.mkdir()
            for name in TEMPLATES:
                shutil.copyfile(SOURCE / "assets/templates" / name, self.bundle / "assets/templates" / name)
            plan = self.call("plan", target, "--language", language)
            self.call("apply", target, "--plan-file", self.save(plan), "--confirmed-plan-digest", plan["plan_digest"])
            for name in TEMPLATES:
                self.assertNotIn("{{", (target / name).read_text(encoding="utf-8"))
            self.assertIn("PROJECT_RULES.md", (target / "AGENTS.md").read_text(encoding="utf-8"))
            repeated = self.call("plan", target, "--language", language)
            self.assertEqual(repeated["blockers"], [])
            self.assertEqual({item["action"] for item in repeated["files"]}, {"keep"})

    def test_english_configuration_renders_without_goal_or_mode(self):
        plan = self.call("plan", self.root, "--language", "english", "--name", "Cancer", "--timezone", "UTC")
        self.assertEqual(set(plan["config"]), {"name", "language", "timezone"})
        self.apply(plan)
        self.assertIn("UTC / English", (self.root / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertNotIn("{{", (self.root / "PROJECT_RULES.md").read_text(encoding="utf-8"))

    def test_hardlinked_agents_rejected(self):
        outside = self.case / "outside.md"
        outside.write_text("must stay unchanged", encoding="utf-8")
        os.link(outside, self.root / "AGENTS.md")
        self.call("plan", self.root, "--language", "chinese", success=False)
        self.assertEqual(outside.read_text(encoding="utf-8"), "must stay unchanged")

    def test_directory_junction_root_rejected(self):
        if os.name != "nt":
            self.skipTest("Windows junction test")
        link = self.case / "linked-root"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(self.root)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.call("plan", link, "--language", "chinese", success=False)
        self.assertEqual(self.snapshot(), {})

    def test_plan_bound_to_original_root(self):
        plan = self.plan()
        other = self.case / "another-root"
        other.mkdir()
        self.call("apply", other, "--plan-file", self.save(plan), "--confirmed-plan-digest", plan["plan_digest"], success=False)
        self.assertEqual(list(other.iterdir()), [])

    def test_old_commands_and_flags_not_accepted(self):
        for args in (("init", self.root), ("resume", self.root), ("plan", self.root, "--language", "chinese", "--mode", "short")):
            result = subprocess.run([sys.executable, "-B", str(self.script), *map(str, args)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.snapshot(), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
