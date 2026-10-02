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
    "scope": "Module documentation",
    "directories": [
        {"path": "module-a/work", "purpose": "Working notes"},
        {"path": "module-a/reference", "purpose": "Original documents"},
        {"path": "module-a/reports", "purpose": "Reports and slide decks"},
    ],
    "environment": None,
    "outputs": ["module-a/reports"],
    "originals": ["module-a/reference"],
}


class RulesTests(unittest.TestCase):
    def setUp(self):
        runs = Path(tempfile.gettempdir()) / "project-directory-organizer-tests"
        runs.mkdir(exist_ok=True)
        self.case = Path(tempfile.mkdtemp(prefix="rules-", dir=runs))
        self.root = self.case / "示例项目"
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
        if args and args[0] == "plan" and "--timezone" not in args:
            args.extend(("--timezone", "UTC"))
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
        self.assertIn("示例项目", (self.root / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertNotIn("{{", (self.root / "AGENTS.md").read_text(encoding="utf-8"))

    def test_plan_without_bytecode_flag_does_not_create_bundle_cache(self):
        environment = dict(os.environ)
        environment.pop("PYTHONDONTWRITEBYTECODE", None)
        result = subprocess.run([sys.executable, "-X", "utf8", str(self.script), "plan", str(self.root), "--language", "chinese", "--timezone", "UTC", "--layout-file", "-"], input=json.dumps(LAYOUT), capture_output=True, encoding="utf-8", env=environment)
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
        self.assertIn(LAYOUT["scope"], text)
        self.assertIn("本次未约定独立环境目录", text)
        self.assertNotIn("env/", text)
        self.assertNotIn("data/raw", text)
        self.assertEqual({p.name for p in self.root.iterdir()}, {"AGENTS.md", "PROJECT_RULES.md"})

    def test_legacy_research_input_normalizes_to_scope_and_applies(self):
        layout = dict(LAYOUT)
        layout["research"] = layout.pop("scope")
        plan = self.call("plan", self.root, "--language", "english", layout=layout)
        self.assertEqual(plan["layout"], LAYOUT)
        self.apply(plan)
        text = (self.root / "PROJECT_RULES.md").read_text(encoding="utf-8")
        self.assertIn("当前工作范围：" + LAYOUT["scope"], text)
        self.assertNotIn("当前研究范围", text)

    def test_layout_with_both_scope_keys_is_rejected_without_writing(self):
        result = self.call("plan", self.root, "--language", "english", layout=dict(LAYOUT, research="Legacy work"), success=False)
        self.assertEqual(result["code"], "invalid_layout")
        self.assertEqual(self.snapshot(), {})

    def test_saved_legacy_plan_is_not_silently_migrated_on_apply(self):
        plan = self.plan()
        plan["layout"]["research"] = plan["layout"].pop("scope")
        payload = {key: value for key, value in plan.items() if key != "plan_digest"}
        plan["plan_digest"] = hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        result = self.apply(plan, success=False)
        self.assertEqual(result["code"], "plan_changed")
        self.assertEqual(self.snapshot(), {})

    def test_timezone_must_be_explicit_before_planning(self):
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", str(self.script), "plan", str(self.root), "--language", "english", "--layout-file", "-"], input=json.dumps(LAYOUT), capture_output=True, encoding="utf-8")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--timezone", result.stderr)
        self.assertEqual(self.snapshot(), {})

    def test_explicit_timezone_label_is_preserved_as_metadata(self):
        plan = self.plan("--timezone", "Local/Configured-Zone")
        self.assertEqual(plan["config"]["timezone"], "Local/Configured-Zone")
        self.apply(plan)
        self.assertIn("Local/Configured-Zone", (self.root / "PROJECT_RULES.md").read_text(encoding="utf-8"))

    def test_chinese_layout_and_environment_are_rendered_with_safe_markdown(self):
        layout = {"scope": "模块一 [初期] 文档整理", "directories": [{"path": "模块一/报告", "purpose": "PDF | PPT 汇报"}, {"path": "环境", "purpose": "项目环境预留"}], "environment": "环境", "outputs": ["模块一/报告"], "originals": []}
        plan = self.call("plan", self.root, "--language", "chinese", layout=layout)
        self.apply(plan)
        text = (self.root / "PROJECT_RULES.md").read_text(encoding="utf-8")
        self.assertIn("模块一/报告", text)
        self.assertIn("PDF \\| PPT", text)
        self.assertIn("模块一 \\[初期\\] 文档整理", text)
        self.assertIn("目录预留不代表已安装", text)
        self.assertNotIn(str(self.root), text)

    def test_unspecified_environment_does_not_deny_existing_native_environment(self):
        native = self.root / ".venv"
        native.mkdir()
        config = native / "pyvenv.cfg"
        config.write_bytes(b"existing native environment")
        plan = self.plan()
        self.apply(plan)
        text = (self.root / "PROJECT_RULES.md").read_text(encoding="utf-8")
        self.assertIn("本次未约定独立环境目录", text)
        self.assertNotIn("当前未设置项目环境", text)
        self.assertIn("已有环境和工具原生环境沿用原位置", text)
        self.assertEqual(config.read_bytes(), b"existing native environment")

    def test_selected_environment_does_not_relocate_native_environments(self):
        layout = dict(LAYOUT, directories=LAYOUT["directories"] + [{"path": "env", "purpose": "Shared environment records"}], environment="env")
        plan = self.call("plan", self.root, "--language", "english", layout=layout)
        self.apply(plan)
        text = (self.root / "PROJECT_RULES.md").read_text(encoding="utf-8")
        self.assertIn("本次约定的独立环境目录为“env”", text)
        self.assertIn("已有环境和工具原生环境沿用原位置", text)
        self.assertNotIn("环境统一归主项目", text)
        self.assertFalse((self.root / "env").exists())

    def test_directory_language_does_not_impose_language_on_existing_filenames(self):
        for name in TEMPLATES:
            shutil.copyfile(SOURCE / "assets/templates" / name, self.bundle / "assets/templates" / name)
        for language in ("chinese", "english"):
            plan = self.call("plan", self.root, "--language", language)
            text = next(item["content"] for item in plan["files"] if item["path"] == "PROJECT_RULES.md")
            self.assertIn("文件名沿用已有项目、工具和原件约定", text)
            self.assertNotIn("文档用简短英文名", text)
            self.assertNotIn("业务目录和文档", text)

    def test_no_outputs_omits_archiving_section(self):
        layout = dict(LAYOUT, outputs=[])
        plan = self.call("plan", self.root, "--language", "english", layout=layout)
        self.apply(plan)
        self.assertNotIn("## 成果归档", (self.root / "PROJECT_RULES.md").read_text(encoding="utf-8"))

    def test_software_project_does_not_invent_archives_or_environment(self):
        layout = {"scope": "Website maintenance", "directories": [{"path": "src", "purpose": "Application source"}, {"path": "public", "purpose": "Stable referenced assets"}, {"path": "dist", "purpose": "Tool-generated build output"}], "environment": None, "outputs": [], "originals": []}
        plan = self.call("plan", self.root, "--language", "english", layout=layout)
        self.apply(plan)
        text = (self.root / "PROJECT_RULES.md").read_text(encoding="utf-8")
        self.assertIn("Website maintenance", text)
        for row in layout["directories"]:
            self.assertIn("| " + row["path"] + " | " + row["purpose"] + " |", text)
        self.assertNotIn("## 成果归档", text)
        self.assertNotIn("YYYYMMDD", text)
        self.assertIn("本次未约定独立环境目录", text)
        self.assertEqual({p.name for p in self.root.iterdir()}, {"AGENTS.md", "PROJECT_RULES.md"})

    def test_archive_rules_apply_only_to_selected_deliveries_and_preserve_tool_contracts(self):
        layout = {"scope": "Website documentation", "directories": [{"path": "dist", "purpose": "Tool-generated build output"}, {"path": "public", "purpose": "Stable referenced assets"}, {"path": "reports", "purpose": "Independent review exports"}], "environment": None, "outputs": ["reports"], "originals": []}
        plan = self.call("plan", self.root, "--language", "english", layout=layout)
        self.apply(plan)
        text = (self.root / "PROJECT_RULES.md").read_text(encoding="utf-8")
        archive = text.split("## 成果归档", 1)[1]
        self.assertIn("独立归档的报告或导出交付件", archive)
        self.assertIn("成果位置：“reports”", archive)
        self.assertNotIn("“dist”", archive)
        self.assertNotIn("“public”", archive)
        self.assertIn("工具固定文件名、产物结构和被引用的稳定资源沿用原约定", archive)
        self.assertIn("已有归档结构沿用", archive)
        self.assertIn("沿用已有命名与版本约定", archive)
        self.assertNotIn("YYYYMMDD", archive)
        self.assertNotIn("v01", archive)

    def test_output_selection_does_not_enable_date_version_or_ascii_naming_defaults(self):
        for name in TEMPLATES:
            shutil.copyfile(SOURCE / "assets/templates" / name, self.bundle / "assets/templates" / name)
        for language in ("chinese", "english"):
            plan = self.call("plan", self.root, "--language", language)
            self.assertEqual(plan["layout"], LAYOUT)
            text = next(item["content"] for item in plan["files"] if item["path"] == "PROJECT_RULES.md")
            self.assertNotIn("YYYYMMDD", text)
            self.assertNotIn("v01", text)
            self.assertNotIn("ASCII", text)

    def test_confirmed_rules_render_safely_apply_and_repeat_without_extra_files(self):
        for name in TEMPLATES:
            shutil.copyfile(SOURCE / "assets/templates" / name, self.bundle / "assets/templates" / name)
        rules = ["Exports use ApprovalDate and Revision A; no date folders.", "Keep [PDF](reports/final.pdf) & <draft> beside the source."]
        layout = dict(LAYOUT, rules=rules)
        plan = self.call("plan", self.root, "--language", "english", layout=layout)
        self.assertEqual(plan["schema_version"], 2)
        self.assertEqual(plan["layout"]["rules"], rules)
        self.apply(plan)
        text = (self.root / "PROJECT_RULES.md").read_text(encoding="utf-8")
        self.assertIn("- Exports use ApprovalDate and Revision A; no date folders.", text)
        self.assertIn("- Keep \\[PDF\\]\\(reports/final.pdf\\) &amp; &lt;draft&gt; beside the source.", text)
        self.assertNotIn("{{", text)
        before = self.snapshot()
        repeated = self.call("plan", self.root, "--language", "english", layout=layout)
        self.assertEqual({item["action"] for item in repeated["files"]}, {"keep"})
        self.apply(repeated)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(set(before), {"AGENTS.md", "PROJECT_RULES.md"})

    def test_selected_rules_never_replace_existing_manual_document(self):
        original = b"\xef\xbb\xbf# Team decisions\r\nExports stay with source files.\r\n"
        (self.root / "PROJECT_RULES.md").write_bytes(original)
        plan = self.call("plan", self.root, "--language", "english", layout=dict(LAYOUT, rules=["Use revision letters."]))
        self.assertEqual(next(item for item in plan["files"] if item["path"] == "PROJECT_RULES.md")["action"], "keep")
        self.apply(plan)
        self.assertEqual((self.root / "PROJECT_RULES.md").read_bytes(), original)

    def test_optional_rules_reject_unsafe_or_unbounded_input_without_writing(self):
        invalid = [None, "A rule", [True], [""], [" first"], ["line\nline"], ["x" * 1001], ["x"] * 501,
                   ["<!-- project-directory-organizer:project:start -->"]]
        for rules in invalid:
            with self.subTest(rules=rules):
                result = self.call("plan", self.root, "--language", "english", layout=dict(LAYOUT, rules=rules), success=False)
                self.assertEqual(result["code"], "invalid_layout")
                self.assertEqual(self.snapshot(), {})

    def test_explicit_empty_rules_remains_valid_with_legacy_scope_alias(self):
        layout = dict(LAYOUT, rules=[])
        layout["research"] = layout.pop("scope")
        plan = self.call("plan", self.root, "--language", "english", layout=layout)
        self.assertEqual(plan["layout"], dict(LAYOUT, rules=[]))
        self.apply(plan)

    def test_invalid_layouts_are_rejected_without_writing(self):
        invalid = []
        for path in ("../outside", "C:/outside", "/absolute", "reports/../outside", "reports/CON.txt", "reports/name.", "reports/name ", "reports/file:stream", "reports/*", "reports//batch"):
            invalid.append(dict(LAYOUT, directories=[{"path": path, "purpose": "Selected"}], outputs=[], originals=[]))
        invalid.extend([
            dict(LAYOUT, directories=[{"path": "Reports", "purpose": "a"}, {"path": "reports", "purpose": "b"}], outputs=[], originals=[]),
            dict(LAYOUT, environment="unselected-env"),
            dict(LAYOUT, outputs=["unselected-reports"]),
            dict(LAYOUT, originals=["../outside"]),
            dict(LAYOUT, scope="scope\nnew instruction"),
            dict(LAYOUT, directories=[{"path": "safe", "purpose": "text\nnew instruction"}], outputs=[], originals=[]),
            dict(LAYOUT, directories="not-a-list"),
            dict(LAYOUT, environment=True),
        ])
        for layout in invalid:
            with self.subTest(layout=layout):
                result = self.call("plan", self.root, "--language", "chinese", layout=layout, success=False)
                self.assertEqual(result["code"], "invalid_layout")
                self.assertEqual(self.snapshot(), {})

    def test_root_rule_names_cannot_be_layout_locations_or_their_ancestors(self):
        for name in ("AGENTS.md", "agents.md", "AgEnTs.Md", "PROJECT_RULES.md", "project_rules.md", "PrOjEcT_RuLeS.Md"):
            for suffix in ("", "/reports"):
                path = name + suffix
                for field in ("directories", "environment", "outputs", "originals"):
                    layout = {"scope": "Selected work", "directories": [], "environment": None, "outputs": [], "originals": []}
                    if field != "originals":
                        layout["directories"] = [{"path": path, "purpose": "Selected location"}]
                    if field == "environment":
                        layout[field] = path
                    elif field in ("outputs", "originals"):
                        layout[field] = [path]
                    with self.subTest(path=path, field=field):
                        result = self.call("plan", self.root, "--language", "english", layout=layout, success=False)
                        self.assertEqual(result["code"], "invalid_layout")
                        self.assertEqual(list(self.root.iterdir()), [])

    def test_rule_names_inside_subprojects_remain_valid_directories_and_original_files(self):
        originals = self.root / "module-b"
        originals.mkdir()
        for name in ("AGENTS.md", "PROJECT_RULES.md"):
            (originals / name).write_bytes(b"Preserved subproject rules")
        layout = {
            "scope": "Selected subprojects",
            "directories": [
                {"path": "module-a/AGENTS.md", "purpose": "Existing naming contract"},
                {"path": "module-a/PROJECT_RULES.md/reports", "purpose": "Independent exports"},
            ],
            "environment": "module-a/AGENTS.md",
            "outputs": ["module-a/PROJECT_RULES.md/reports"],
            "originals": ["module-b/AGENTS.md", "module-b/PROJECT_RULES.md"],
        }
        plan = self.call("plan", self.root, "--language", "english", layout=layout)
        self.assertTrue(plan["ready_to_apply"])
        self.assertEqual(plan["layout"], layout)
        self.apply(plan)
        for name in ("AGENTS.md", "PROJECT_RULES.md"):
            self.assertEqual((originals / name).read_bytes(), b"Preserved subproject rules")
            self.assertTrue((self.root / name).is_file())
        for path in ("module-a/AGENTS.md", "module-a/PROJECT_RULES.md/reports"):
            selected = self.root / path
            self.assertFalse(selected.exists())
            selected.mkdir(parents=True)
            self.assertTrue(selected.is_dir())

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
        layout = dict(LAYOUT, directories=[{"path": "module-a/data", "purpose": "Selected data"}], outputs=[], originals=["module-a/data/raw"])
        plan = self.call("plan", self.root, "--language", "english", layout=layout)
        self.apply(plan)
        self.assertIn("module-a/data/raw", (self.root / "PROJECT_RULES.md").read_text(encoding="utf-8"))
        self.assertFalse((self.root / "module-a").exists())

    def test_root_level_original_file_does_not_require_a_made_up_directory(self):
        original = self.root / "contract.pdf"
        original.write_bytes(b"original document bytes")
        layout = dict(LAYOUT, directories=[], outputs=[], originals=["contract.pdf"])
        plan = self.call("plan", self.root, "--language", "english", layout=layout)
        self.apply(plan)
        self.assertEqual(original.read_bytes(), b"original document bytes")
        text = (self.root / "PROJECT_RULES.md").read_text(encoding="utf-8")
        self.assertIn("原件位置：“contract.pdf”", text)
        self.assertIn("明确保留的输入原件", text)
        self.assertIn("日常维护文件按任务与版本控制约定编辑", text)
        self.assertEqual({p.name for p in self.root.iterdir()}, {"AGENTS.md", "PROJECT_RULES.md", "contract.pdf"})

    def test_original_and_output_locations_must_not_overlap(self):
        for original, output in (("data", "data"), ("data", "data/results"), ("data/raw", "data")):
            layout = {"scope": "Selected work", "directories": [{"path": "data", "purpose": "Data"}, {"path": "data/results", "purpose": "Results"}], "environment": None, "outputs": [output], "originals": [original]}
            with self.subTest(original=original, output=output):
                result = self.call("plan", self.root, "--language", "english", layout=layout, success=False)
                self.assertEqual(result["code"], "invalid_layout")
                self.assertEqual(self.snapshot(), {})

    def test_explicit_date_and_version_choices_are_preserved_in_both_languages(self):
        for name in TEMPLATES:
            shutil.copyfile(SOURCE / "assets/templates" / name, self.bundle / "assets/templates" / name)
        for language, rule in (("english", "Export batches use YYYYMMDD-purpose and revisions start at v01."),
                               ("chinese", "交付批次采用 YYYYMMDD用途，版本从 v01 递增。")):
            plan = self.call("plan", self.root, "--language", language, layout=dict(LAYOUT, rules=[rule]))
            text = next(item["content"] for item in plan["files"] if item["path"] == "PROJECT_RULES.md")
            self.assertEqual(plan["layout"]["rules"], [rule])
            self.assertIn("- " + rule, text)

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
        (self.root / "module-a").write_bytes(b"a file")
        result = self.call("plan", self.root, "--language", "chinese", success=False)
        self.assertEqual(result["code"], "invalid_layout")
        self.assertEqual((self.root / "module-a").read_bytes(), b"a file")

    def test_duplicate_layout_json_keys_are_rejected(self):
        source = '{"scope":"first","scope":"second","directories":[],"environment":null,"outputs":[],"originals":[]}'
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
        plan = self.call("plan", self.root, "--language", "english", "--name", "Example Project", "--timezone", "UTC")
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
