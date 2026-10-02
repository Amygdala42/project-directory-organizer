#!/usr/bin/env python3
"""Plan the two project rule documents and write them on explicit request.

Python 3.9+, standard library only. The assistant presents the complete plan
in its final reply and ends the turn. An explicit operation request authorizes
writing; the CLI digest verifies plan integrity, not user authorization.
There is no hidden project configuration or bytecode cache.
Filesystem checks protect normal concurrent use, not hostile replacement races.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import unicodedata

sys.dont_write_bytecode = True

from path_safety import RootGuard, OperationError, fail, identity, is_reparse


TOOL = "codex-project-rules"
FILES = ("PROJECT_RULES.md", "AGENTS.md")
TOKEN = re.compile(r"\{\{([A-Z_]+)\}\}")
START = "<!-- project-directory-organizer:rules:start -->"
END = "<!-- project-directory-organizer:rules:end -->"
PROJECT_MARKER = "project-directory-organizer:project"
PROJECT_ID = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
PROJECT_START = re.compile(r"<!-- project-directory-organizer:project:start id=(" + PROJECT_ID + r") path=(.+) sha256=([0-9a-f]{64}) -->")
PROJECT_END = re.compile(r"<!-- project-directory-organizer:project:end id=(" + PROJECT_ID + r") -->")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def plan_digest(plan):
    payload = {key: value for key, value in plan.items() if key != "plan_digest"}
    return digest(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def validate_config(config):
    if not isinstance(config, dict) or set(config) != {"name", "timezone", "language"}:
        fail("invalid_config", "Configuration must contain name, timezone, and language.")
    for key, value in config.items():
        if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 200:
            fail("invalid_config", key + " must be nonempty text of at most 200 characters without surrounding whitespace.")
        if any(unicodedata.category(char).startswith("C") or char in "\u2028\u2029" for char in value):
            fail("invalid_config", key + " must be a single line without control characters.")
    if config["language"] not in {"chinese", "english"}:
        fail("invalid_config", "language must be chinese or english.")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_+.-]*(?:/[A-Za-z0-9_+.-]+)*", config["timezone"]):
        fail("invalid_config", "Use an explicit timezone label such as UTC or a Region/City label.")
    return config


def layout_text(value, field):
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 1000:
        fail("invalid_layout", field + " must be nonempty text of at most 1000 characters without surrounding whitespace.")
    if any(unicodedata.category(char).startswith("C") or char in "\u2028\u2029" for char in value):
        fail("invalid_layout", field + " must be a single line without control characters.")
    return value


def relative_layout_path(value, guard, field, allow_file=False):
    value = layout_text(value, field).replace("\\", "/")
    parts = value.split("/")
    if any(part in {"", ".", ".."} or part != part.strip() or part.endswith(".") or len(part) > 255 for part in parts):
        fail("invalid_layout", field + " must be a project-relative path with ordinary named components.")
    if any(char in value for char in '<>:"|?*'):
        fail("invalid_layout", field + " contains a forbidden path character.")
    for part in parts:
        base = part.split(".", 1)[0].upper()
        if base in {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"} or re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", base):
            fail("invalid_layout", field + " contains a reserved device name.")
    if parts[0].casefold() in {"agents.md", "project_rules.md"}:
        fail("invalid_layout", field + " conflicts with a project rule document.")
    current = guard.path
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if is_reparse(info):
            fail("invalid_layout", field + " passes through a symlink, junction, or reparse point.")
        if not stat.S_ISDIR(info.st_mode) and not (allow_file and index == len(parts) - 1 and stat.S_ISREG(info.st_mode)):
            fail("invalid_layout", field + " conflicts with an existing non-directory item.")
    guard.check()
    return value


def path_key(value):
    return unicodedata.normalize("NFC", value).casefold()


def validate_layout(layout, guard):
    fields = {"directories", "environment", "outputs", "originals"}
    if not isinstance(layout, dict) or set(layout) - {"rules"} not in (fields | {"scope"}, fields | {"research"}):
        fail("invalid_layout", "Layout must contain scope, directories, environment, outputs, and originals; rules is optional. The legacy research key may replace scope, but cannot appear alongside it.")
    scope_field = "scope" if "scope" in layout else "research"
    scope = layout_text(layout[scope_field], scope_field)
    rows = layout["directories"]
    if not isinstance(rows, list) or len(rows) > 500:
        fail("invalid_layout", "directories must be a list of at most 500 selected directory rows.")
    directories, selected = [], set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "purpose"}:
            fail("invalid_layout", "Each selected directory must contain only path and purpose.")
        path = relative_layout_path(row["path"], guard, "directory path")
        if path_key(path) in selected:
            fail("invalid_layout", "Selected directory paths must be unique, ignoring case and Unicode normalization.")
        selected.add(path_key(path))
        directories.append({"path": path, "purpose": layout_text(row["purpose"], "directory purpose")})
    environment = layout["environment"]
    if environment is not None:
        environment = relative_layout_path(environment, guard, "environment")
        if path_key(environment) not in selected:
            fail("invalid_layout", "The environment must be explicitly listed among selected directories.")
    lists = {}
    for field in ("outputs", "originals"):
        values = layout[field]
        if not isinstance(values, list) or len(values) > 500:
            fail("invalid_layout", field + " must be a list of at most 500 project-relative paths.")
        paths, seen = [], set()
        for value in values:
            path = relative_layout_path(value, guard, field, allow_file=field == "originals")
            key = path_key(path)
            if key in seen:
                fail("invalid_layout", field + " must not repeat the same path.")
            seen.add(key)
            if field == "outputs" and key not in selected:
                fail("invalid_layout", "Each output location must be explicitly listed among selected directories.")
            paths.append(path)
        lists[field] = paths
    for original in lists["originals"]:
        for output in lists["outputs"]:
            a, b = path_key(original), path_key(output)
            if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                fail("invalid_layout", "Original-material and output locations must not overlap or contain one another.")
    result = {"scope": scope, "directories": directories, "environment": environment, **lists}
    if "rules" in layout:
        rules = layout["rules"]
        if not isinstance(rules, list) or len(rules) > 500:
            fail("invalid_layout", "rules must be a list of at most 500 confirmed single-line rules.")
        rules = [layout_text(rule, "rule") for rule in rules]
        if any("project-directory-organizer:" in rule.lower() for rule in rules):
            fail("invalid_layout", "rules must not contain reserved rule markers.")
        result["rules"] = rules
    return result


def markdown_text(value):
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_{}\[\]()|#])", r"\\\1", value)


def quoted_path(value):
    return "“" + markdown_text(value) + "”"


def layout_fields(layout, language):
    rows = layout["directories"]
    table = "| 相对目录 | 用途与收纳边界 |\n| --- | --- |\n" + "\n".join("| " + markdown_text(row["path"]) + " | " + markdown_text(row["purpose"]) + " |" for row in rows) if rows else "本次未选业务子目录。"
    environment = "本次未约定独立环境目录；已有环境和工具原生环境沿用原位置。" if layout["environment"] is None else "本次约定的独立环境目录为" + quoted_path(layout["environment"]) + "；目录预留不代表已安装，已有环境和工具原生环境沿用原位置，实际配置按任务与工具要求确定。"
    outputs = ""
    if layout["outputs"]:
        locations = "、".join(quoted_path(path) for path in layout["outputs"])
        outputs = "\n".join((
            "## 成果归档", "",
            "- 成果位置：" + locations + "。",
            "- 以下约定适用于这些位置中独立归档的报告或导出交付件；工具固定文件名、产物结构和被引用的稳定资源沿用原约定。",
            "- 已有归档结构沿用，独立交付文件沿用已有命名与版本约定。日期、编号、版本和批次层级仅按本项目明确采用的规则使用，不因选择成果位置自动增加。",
            "- 成果、阅读说明及明确交付附件按用途与状态组织；制作过程文件另存，用链接、同名或批次标识保持关联。修订不得静默覆盖已有内容，不为填目录而复制未变化材料。",
            "- 混合格式不要求另建目录；明确需要自包含交付包时，在约定位置复制交付范围内的必要依赖为快照并注明维护源，不形成第二维护主库。",
            "- 稳定维护的代码、数据和参考资料不随日期移动，稳定引用沿用原位置。",
        ))
    originals = "明确保留的输入原件保持内容、名称、位置和包结构；日常维护文件按任务与版本控制约定编辑。"
    if layout["originals"]:
        originals = "原件位置：" + "、".join(quoted_path(path) for path in layout["originals"]) + "。" + originals
    return {"PROJECT_SCOPE": "本文件所在目录为主项目根目录，下列路径均相对此目录。当前工作范围：" + markdown_text(layout["scope"]) + "。", "DIRECTORY_TABLE": table, "ENVIRONMENT_RULE": environment, "OUTPUT_RULES": outputs, "ORIGINAL_RULES": originals,
            "CONFIRMED_RULES": "\n".join("- " + markdown_text(rule) for rule in layout.get("rules", []))}


def templates(config, layout, include_rules):
    values = {
        "PROJECT_NAME": config["name"], "TIMEZONE": config["timezone"],
        "LANGUAGE": "中文" if config["language"] == "chinese" else "English",
        "RULES_FILE": "PROJECT_RULES.md",
        "NAMING_RULE": ("自建业务目录用清楚、简短的中文名称。" if config["language"] == "chinese" else "自建业务目录用清楚、简短的英文名称。") + "同一层级保持风格一致；文件名沿用已有项目、工具和原件约定，日期、编号、分隔方式和版本格式按项目实际采用的规则确定。",
    }
    if layout is not None:
        values.update(layout_fields(layout, config["language"]))
    folder = Path(__file__).resolve().parent.parent / "assets/templates"
    rendered, hashes = {}, {}
    for name in FILES if include_rules else ("AGENTS.md",):
        raw = (folder / name).read_bytes()
        try:
            source = raw.decode("utf-8-sig")
        except UnicodeError:
            fail("template_error", "Template must be UTF-8: " + name)
        unknown = set(TOKEN.findall(source)) - set(values)
        if unknown:
            fail("template_error", "Unrecognized template fields in " + name + ": " + ", ".join(sorted(unknown)))
        rendered[name] = TOKEN.sub(lambda match: values[match.group(1)], source)
        hashes[name] = digest(raw)
    return rendered, hashes


def managed_block(template):
    normalized = template.replace("\r\n", "\n")
    if normalized.count(START) != 1 or normalized.count(END) != 1:
        fail("template_error", "AGENTS.md template must have exactly one managed rules paragraph.")
    start, end = normalized.index(START), normalized.index(END)
    if end < start:
        fail("template_error", "Managed rules paragraph markers are out of order.")
    return normalized[start:end + len(END)]


def agents_literal_context(text):
    """Spot common fenced/indented literal contexts; do not parse Markdown."""
    fence = None
    for original in text.splitlines():
        line = original.expandtabs(4)
        marker = START in line or END in line
        if marker and (fence is not None or line.startswith("    ") or line.strip() not in {START, END}):
            return "A managed rules marker may be inside a code block or other literal text."
        if fence is not None:
            character, minimum = fence
            if re.fullmatch(r" {0,3}" + re.escape(character) + "{" + str(minimum) + r",}[ \t]*", line):
                fence = None
            continue
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if opening:
            fence = (opening.group(1)[0], len(opening.group(1)))
    if fence is not None:
        return "An unclosed Markdown code fence would make appended project rules literal text."
    return None


def read_item(guard, name):
    info = guard.item(name, "file")
    if info is None:
        return None, None
    if info.st_nlink != 1:
        fail("hardlink", "Rule documents must not have hard links: " + name)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(guard.path / name, flags)
    try:
        opened = os.fstat(fd)
        if is_reparse(opened) or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or identity(opened) != identity(info):
            fail("item_changed", "Rule document changed while opening: " + name)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read()
        after = os.fstat(fd)
        current = guard.item(name, "file")
        if current is None or identity(current) != identity(opened) or (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
            fail("item_changed", "Rule document changed while reading: " + name)
        snapshot = {"identity": list(identity(opened)), "size": len(raw), "mtime_ns": opened.st_mtime_ns, "sha256": digest(raw)}
        return raw, snapshot
    finally:
        os.close(fd)


def make_plan(root, config, layout=None):
    config = validate_config(config)
    guard = RootGuard(root)
    existing = {name: read_item(guard, name) for name in FILES}
    include_rules = existing["PROJECT_RULES.md"][0] is None
    if layout is not None:
        layout = validate_layout(layout, guard)
    elif include_rules:
        fail("layout_required", "Creating PROJECT_RULES.md requires an explicit selected layout via --layout-file; do not fill it with generic directory candidates.")
    rendered, template_hashes = templates(config, layout, include_rules)
    block = managed_block(rendered["AGENTS.md"])
    files, blockers = [], []
    for name in FILES:
        raw, before = existing[name]
        item = {"path": name, "absolute_path": str(guard.path / name), "before": before, "action": "keep", "content": ""}
        if raw is None:
            item.update(action="create", content=rendered[name], after_sha256=digest(rendered[name].encode("utf-8")))
        else:
            item["after_sha256"] = digest(raw)
            if name == "PROJECT_RULES.md":
                files.append(item)
                continue
            try:
                current = raw.decode("utf-8-sig")
            except UnicodeError:
                fail("encoding", "Existing rule document is not UTF-8; preserve it and merge manually: " + name)
            literal_context = agents_literal_context(current) if name == "AGENTS.md" else None
            if literal_context:
                blockers.append({"path": name, "code": "manual_merge_required", "reason": literal_context + " Preserve the original bytes; the assistant must review the Markdown context and include an exact merge diff in the plan before any user-requested edit."})
            elif current == rendered[name]:
                pass
            else:
                normalized = current.replace("\r\n", "\n")
                if normalized.count(START) == 1 and normalized.count(END) == 1 and block in normalized:
                    pass
                elif START in current or END in current or re.search(r"project_rules\.md", current, re.IGNORECASE):
                    blockers.append({"path": name, "code": "manual_merge_required", "reason": "Existing rule link or managed paragraph needs review. Do not append a duplicate; include an exact merge diff in the plan before any user-requested edit."})
                else:
                    addition = ("\n" if raw.endswith(b"\n") else "\n\n") + block + "\n"
                    item.update(action="append", content=addition, after_sha256=digest(raw + addition.encode("utf-8")))
        files.append(item)
    guard.check()
    plan = {"tool": TOOL, "schema_version": 2, "root": str(guard.path), "root_identity": list(identity(guard.path.lstat())), "config": config, "layout": layout, "template_sha256": template_hashes, "files": files, "blockers": blockers, "ready_to_apply": not blockers}
    plan["plan_digest"] = plan_digest(plan)
    return plan


def registry_text(value, field):
    value = layout_text(value, field)
    if "project-directory-organizer:" in value.lower():
        fail("invalid_projects", field + " must not contain reserved rule markers.")
    return value


def paths_overlap(first, second):
    first, second = path_key(first), path_key(second)
    return first == second or first.startswith(second + "/") or second.startswith(first + "/")


def project_environment(value, identifier, path, guard):
    if value is None:
        return None
    environment = relative_layout_path(value, guard, "environment")
    central, key = path_key("env/" + identifier), path_key(environment)
    if key == path_key(path) or path_key(path).startswith(key + "/"):
        fail("invalid_projects", "An environment must not equal or contain its own project root.")
    if not (key.startswith(path_key(path) + "/") or key == central or key.startswith(central + "/")):
        fail("invalid_projects", "An environment must use env/<project id> or a descendant, or remain strictly inside its own project root.")
    return environment


def project_location_conflicts(records):
    """Compare complete proposed ownership, including unchanged registrations."""
    for index, first in enumerate(records):
        for second in records[index + 1:]:
            if paths_overlap(first["path"], second["path"]):
                yield "Independent project roots must not overlap: " + first["id"] + " and " + second["id"]
            for owner, other in ((first, second), (second, first)):
                environment = owner["environment"]
                # Outputs remain strictly inside their owner's root, so rejecting
                # all root overlap also protects every output without decoding
                # the older human-readable, semicolon-delimited Outputs row.
                if environment is not None and paths_overlap(environment, other["path"]):
                    yield "An environment overlaps another project's root or outputs: " + owner["id"] + " and " + other["id"]
            if first["environment"] is not None and second["environment"] is not None and paths_overlap(first["environment"], second["environment"]):
                yield "Project environments must not overlap or contain one another: " + first["id"] + " and " + second["id"]


def validate_projects(request, guard):
    if not isinstance(request, dict) or not set(request) <= {"projects", "initialize_after"} or "projects" not in request:
        fail("invalid_projects", "Input must contain projects and optionally initialize_after.")
    rows = request["projects"]
    if not isinstance(rows, list) or not rows or len(rows) > 500:
        fail("invalid_projects", "projects must contain 1 to 500 explicitly confirmed project records.")
    fields = {"id", "path", "purpose", "status", "language", "environment", "outputs", "rules"}
    records, identifiers, paths = [], set(), []
    for row in rows:
        if not isinstance(row, dict) or set(row) not in (fields, fields | {"before"}):
            fail("invalid_projects", "Each project needs id, path, purpose, status, language, environment, outputs, and rules; before is optional.")
        identifier = row["id"]
        if not isinstance(identifier, str) or re.fullmatch(PROJECT_ID, identifier) is None or identifier.casefold() in identifiers:
            fail("invalid_projects", "Project ids must be unique ASCII letters, numbers, underscores, or hyphens, starting with a letter or number, at most 64 characters.")
        identifiers.add(identifier.casefold())
        path = relative_layout_path(row["path"], guard, "project path")
        if any(paths_overlap(path, other) for other in paths):
            fail("invalid_projects", "Independent project roots must not overlap or contain one another.")
        paths.append(path)
        if row["status"] not in ("planned", "active", "archived") or row["language"] not in ("chinese", "english"):
            fail("invalid_projects", "status must be planned/active/archived and language must be chinese/english.")
        environment = project_environment(row["environment"], identifier, path, guard)
        outputs, seen = [], set()
        if not isinstance(row["outputs"], list) or len(row["outputs"]) > 500:
            fail("invalid_projects", "outputs must be a list of at most 500 root-relative locations.")
        for output in row["outputs"]:
            output = relative_layout_path(output, guard, "output")
            if path_key(output) in seen:
                fail("invalid_projects", "Output paths must be unique.")
            seen.add(path_key(output))
            outputs.append(output)
        for output in outputs:
            if not path_key(output).startswith(path_key(path) + "/"):
                fail("invalid_projects", "Output locations must be strictly inside their own project root, using full root-relative paths.")
        if environment is not None and any(paths_overlap(environment, output) for output in outputs):
            fail("invalid_projects", "Environment and output locations must not overlap.")
        rules = row["rules"]
        if not isinstance(rules, list) or len(rules) > 500:
            fail("invalid_projects", "rules must be a list of at most 500 single-line rules.")
        record = {"id": identifier, "path": path, "purpose": registry_text(row["purpose"], "purpose"),
                  "status": row["status"], "language": row["language"], "environment": environment,
                  "outputs": outputs, "rules": [registry_text(rule, "rule") for rule in rules]}
        if "before" in row:
            if not isinstance(row["before"], str) or not row["before"] or PROJECT_MARKER in row["before"]:
                fail("invalid_projects", "before must be nonempty exact legacy text without managed project markers.")
            record["before"] = row["before"]
        records.append(record)
    for reason in project_location_conflicts(records):
        fail("invalid_projects", reason)
    result = {"projects": records}
    if "initialize_after" in request:
        if not isinstance(request["initialize_after"], str) or PROJECT_MARKER in request["initialize_after"]:
            fail("invalid_projects", "initialize_after must be exact existing text without managed project markers.")
        result["initialize_after"] = request["initialize_after"]
    return result


def project_block(record, newline):
    """Render one independently owned record; its digest covers exact body bytes."""
    lines = ["## Project " + record["id"], "", "| Field | Confirmed value |", "| --- | --- |",
             "| Project id | " + record["id"] + " |", "| Root-relative path | " + markdown_text(record["path"]) + " |",
             "| Purpose | " + markdown_text(record["purpose"]) + " |", "| Status | " + record["status"] + " |",
             "| Directory language | " + record["language"] + " |",
             "| Environment | " + (markdown_text(record["environment"]) if record["environment"] else "None selected; existing tool locations remain unchanged.") + " |",
             "| Outputs | " + ("; ".join(markdown_text(value) for value in record["outputs"]) or "None selected.") + " |", ""]
    lines.extend("- " + markdown_text(rule) for rule in record["rules"])
    if not record["rules"]:
        lines.append("No additional project-specific rules confirmed.")
    body = (newline.join(lines) + newline).encode("utf-8")
    start = "<!-- project-directory-organizer:project:start id=" + record["id"] + " path=" + record["path"] + " sha256=" + digest(body) + " -->"
    end = "<!-- project-directory-organizer:project:end id=" + record["id"] + " -->"
    return (start + newline).encode("utf-8") + body + (end + newline).encode("utf-8")


def registered_environment(body_lines, record, guard):
    """Recover only the unambiguous environment field from the existing format."""
    lines = [line for line in body_lines if re.match(r"^\s*\|\s*Environment\s*\|", line, re.IGNORECASE)]
    prefix, suffix = "| Environment | ", " |"
    if len(lines) != 1 or not lines[0].startswith(prefix) or not lines[0].endswith(suffix):
        fail("invalid_projects", "The registered environment field is missing, duplicated, or unknown.")
    encoded = lines[0][len(prefix):-len(suffix)]
    if encoded == "None selected; existing tool locations remain unchanged.":
        return None
    value = re.sub(r"\\([\\`*_{}\[\]()|#])", r"\1", encoded)
    value = value.replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&")
    environment = project_environment(value, record["id"], record["path"], guard)
    if markdown_text(environment) != encoded:
        fail("invalid_projects", "The registered environment path is not canonically encoded.")
    return environment


def scan_project_blocks(raw, guard):
    """Conservative line scanner: marker ambiguity is a blocker, never recovery."""
    blocks, blockers, literal_ranges = [], [], []
    fence, opened, offset = None, None, 0
    identifiers, paths = set(), []

    def block(code, reason):
        blockers.append({"path": "PROJECT_RULES.md", "code": code, "reason": reason})

    for line in raw.splitlines(keepends=True):
        end = offset + len(line)
        text = line.decode("utf-8").rstrip("\r\n")
        if offset == 0:
            text = text.lstrip("\ufeff")
        expanded = text.expandtabs(4)
        was_fenced = fence is not None
        literal = was_fenced or expanded.startswith("    ")
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", expanded)
        if was_fenced:
            character, minimum = fence
            if re.fullmatch(r" {0,3}" + re.escape(character) + "{" + str(minimum) + r",}[ \t]*", expanded):
                fence = None
        elif opening:
            fence = (opening.group(1)[0], len(opening.group(1)))
            literal = True
        if literal:
            literal_ranges.append((offset, end))
        if PROJECT_MARKER in text:
            start_match, end_match = PROJECT_START.fullmatch(text), PROJECT_END.fullmatch(text)
            if literal or not (start_match or end_match):
                block("manual_merge_required", "A project marker is damaged, embedded in text, or inside a code block.")
            elif start_match:
                identifier, path, checksum = start_match.groups()
                if opened is not None:
                    block("manual_merge_required", "Managed project blocks must not be nested.")
                if identifier.casefold() in identifiers:
                    block("manual_merge_required", "A managed project id appears more than once: " + identifier)
                identifiers.add(identifier.casefold())
                try:
                    normalized = relative_layout_path(path, guard, "registered project path")
                    if normalized != path or any(paths_overlap(path, other) for other in paths):
                        block("manual_merge_required", "Existing project root registrations overlap or are not canonical.")
                    paths.append(path)
                except OperationError as error:
                    block("manual_merge_required", "Existing registered project path needs review: " + str(error))
                marker_offset = offset + (3 if offset == 0 and line.startswith(b"\xef\xbb\xbf") else 0)
                opened = {"id": identifier, "path": path, "sha256": checksum, "start": marker_offset, "body_start": end,
                          "newline": "\r\n" if line.endswith(b"\r\n") else "\n"}
            else:
                identifier = end_match.group(1)
                if opened is None or identifier != opened["id"]:
                    block("manual_merge_required", "Managed project start/end markers are missing or mismatched.")
                else:
                    body = raw[opened["body_start"]:offset]
                    if digest(body) != opened["sha256"]:
                        block("managed_content_changed", "Generated body was edited manually; preserve it and merge explicitly: " + identifier)
                    expected_newline = opened["newline"].encode("ascii")
                    managed_lines = raw[opened["start"]:end].splitlines(keepends=True)
                    if any(part[len(part.rstrip(b"\r\n")):] != expected_newline for part in managed_lines):
                        block("managed_content_changed", "Managed markers and body must use the same LF or CRLF line endings; preserve the edited bytes: " + identifier)
                    body_lines = body.decode("utf-8").splitlines()
                    if not body_lines or body_lines[0] != "## Project " + identifier or "| Project id | " + identifier + " |" not in body_lines or "| Root-relative path | " + markdown_text(opened["path"]) + " |" not in body_lines:
                        block("manual_merge_required", "Managed project identity/path markers disagree with their body: " + identifier)
                    try:
                        opened["environment"] = registered_environment(body_lines, opened, guard)
                    except OperationError as error:
                        block("manual_merge_required", "Existing registered environment needs review: " + identifier + ": " + str(error))
                    opened["end"] = end
                    blocks.append(opened)
                opened = None
        offset = end
    if opened is not None:
        block("manual_merge_required", "A managed project block is missing its end marker.")
    if fence is not None:
        block("manual_merge_required", "An unclosed Markdown fence makes safe project insertion ambiguous.")
    return blocks, blockers, literal_ranges


def exact_text_span(raw, text, literal_ranges):
    data = text.encode("utf-8")
    if not data or raw.count(data) != 1:
        return None
    start = raw.index(data)
    end = start + len(data)
    if (start and raw[start - 1:start] != b"\n") or (end != len(raw) and raw[end - 1:end] != b"\n"):
        return None
    if any(start < last and end > first for first, last in literal_ranges):
        return None
    return start, end


def make_update_plan(root, request):
    guard = RootGuard(root)
    request = validate_projects(request, guard)
    raw, before = read_item(guard, "PROJECT_RULES.md")
    if raw is None:
        fail("rules_required", "Incremental update requires an existing PROJECT_RULES.md; create the base documents with plan first.")
    try:
        original = raw.decode("utf-8")
    except UnicodeError:
        fail("encoding", "Existing PROJECT_RULES.md is not UTF-8; preserve it and merge manually.")
    blocks, blockers, literal_ranges = scan_project_blocks(raw, guard)

    def block(reason, code="manual_merge_required"):
        blockers.append({"path": "PROJECT_RULES.md", "code": code, "reason": reason})

    by_id = {item["id"].casefold(): item for item in blocks}
    newline = "\r\n" if b"\r\n" in raw else "\n"
    edits, additions, migrated_spans = [], [], []
    for record in request["projects"]:
        existing = by_id.get(record["id"].casefold())
        if existing is not None:
            if "before" in record:
                block("before cannot replace an already managed project; preserve and review its current marked body.")
            if existing["id"] != record["id"]:
                block("An existing project id differs only in case; do not silently change its identity.")
            start, end = existing["start"], existing["end"]
            replacement = project_block(record, existing["newline"])
            if raw[start:end] != replacement:
                edits.append((start, end, replacement))
        elif "before" in record:
            span = exact_text_span(raw, record["before"], literal_ranges)
            if span is None or span == (0, len(raw)):
                block("before must identify one exact complete legacy section outside code, and must not replace the entire document.")
            elif any(span[0] < item["end"] and span[1] > item["start"] for item in blocks):
                block("Legacy migration overlaps an existing managed project.")
            else:
                migrated_spans.append(span)
                edits.append((*span, project_block(record, newline)))
        else:
            additions.append(project_block(record, newline))

    # Existing blocks and explicit legacy bindings are the only owned spans.
    owned = sorted([(item["start"], item["end"]) for item in blocks] + migrated_spans)
    outside, previous = [], 0
    for start, end in owned:
        if start < previous:
            block("Explicit legacy project sections overlap; review the bindings manually.")
        outside.append(raw[previous:start])
        previous = end
    outside.append(raw[previous:])
    unmanaged = b"\n".join(outside).decode("utf-8")
    if re.search(r"(?im)^\s*(?:#{1,6}\s*)?(?:子项目登记|项目登记|subproject registry|project registry|projects registry|registered projects|project register)", unmanaged):
        block("An unmanaged project registry already exists; migrate its exact project sections without creating a second registry.")
    for record in request["projects"]:
        for value in (record["id"], record["path"]):
            reference = re.escape(value).replace("/", r"[/\\]")
            if re.search(r"(?<![\w/\\-])" + reference + r"(?![\w-])", unmanaged, re.IGNORECASE):
                block("An unmanaged reference to this project needs an exact legacy binding before registration: " + record["id"])
                break

    # A missing environment field has already produced a blocker above; never
    # allow an update (even to that same record) to bypass unknown registrations.
    resulting = {item["id"].casefold(): item for item in blocks if "environment" in item}
    resulting.update({row["id"].casefold(): row for row in request["projects"]})
    for reason in project_location_conflicts(list(resulting.values())):
        block(reason)

    if "initialize_after" in request and (blocks or migrated_spans):
        block("initialize_after is only for the first project area, without existing managed or legacy-migration blocks.")
    if additions:
        if blocks:
            insertion = max(item["end"] for item in blocks)
        elif "initialize_after" in request:
            anchor = request["initialize_after"]
            span = exact_text_span(raw, anchor, literal_ranges)
            if raw == b"" and anchor == "":
                insertion = 0
            elif span is None or not anchor.endswith("\n"):
                block("initialize_after must identify unique exact complete text ending in a newline, outside code.")
                insertion = None
            else:
                insertion = span[1]
        elif migrated_spans:
            block("Migrate existing sections first; add new projects with a fresh subsequent plan.")
            insertion = None
        else:
            block("No managed project area exists. Bind exact legacy sections with before, or explicitly initialize a verified empty registry with initialize_after.")
            insertion = None
        if insertion is not None:
            prefix = newline.encode("utf-8") if insertion and raw[insertion - 1:insertion] == b"\n" else (newline * 2).encode("utf-8") if insertion else b""
            edits.append((insertion, insertion, prefix + newline.encode("utf-8").join(additions)))
    elif "initialize_after" in request:
        block("initialize_after requires new, unmanaged project records.")

    result = raw
    if not blockers:
        for start, end, replacement in sorted(edits, reverse=True):
            result = result[:start] + replacement + result[end:]
    guard.check()
    changed = result != raw
    item = {"path": "PROJECT_RULES.md", "absolute_path": str(guard.path / "PROJECT_RULES.md"), "before": before,
            "action": "replace" if changed else "keep", "content": result.decode("utf-8") if changed else "",
            "after_sha256": digest(result)}
    # Full context keeps every unchanged project and manual paragraph reviewable.
    diff = "".join(difflib.unified_diff(original.splitlines(keepends=True), result.decode("utf-8").splitlines(keepends=True),
                                      fromfile="PROJECT_RULES.md (before)", tofile="PROJECT_RULES.md (after)",
                                      n=max(len(original.splitlines()), len(result.splitlines())))) if changed else ""
    plan = {"tool": TOOL, "schema_version": 3, "operation": "update-projects", "root": str(guard.path),
            "root_identity": list(identity(guard.path.lstat())), "request": request, "files": [item],
            "diff": diff, "blockers": blockers, "ready_to_apply": not blockers}
    plan["plan_digest"] = plan_digest(plan)
    return plan


@contextmanager
def exclusive_file(fd, size):
    """Use a temporary OS lock on the existing file; no lock/config files."""
    os.lseek(fd, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, max(1, size))
        except OSError:
            fail("file_busy", "An existing rule document is locked or being updated.")
        try:
            yield
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, max(1, size))
    else:
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fail("file_busy", "An existing rule document is locked or being updated.")
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


def load_plan(value):
    if value == "-":
        source = sys.stdin.read()
    else:
        source = Path(value).read_text(encoding="utf-8-sig")
    try:
        plan = json.loads(source)
    except (ValueError, RecursionError):
        fail("invalid_plan", "Plan must be a valid JSON object from the plan command.")
    if not isinstance(plan, dict):
        fail("invalid_plan", "Plan must be a JSON object.")
    return plan


def load_layout(value):
    source = sys.stdin.read() if value == "-" else Path(value).read_text(encoding="utf-8-sig")
    def unique_keys(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                fail("invalid_layout", "Layout JSON must not repeat an object key: " + key)
            result[key] = item
        return result
    try:
        return json.loads(source, object_pairs_hook=unique_keys)
    except (ValueError, RecursionError):
        fail("invalid_layout", "Layout must be valid JSON describing the selected structure.")


def load_projects(value):
    source = sys.stdin.read() if value == "-" else Path(value).read_text(encoding="utf-8-sig")
    def unique_keys(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                fail("invalid_projects", "Project JSON must not repeat an object key: " + key)
            result[key] = item
        return result
    try:
        return json.loads(source, object_pairs_hook=unique_keys)
    except (ValueError, RecursionError):
        fail("invalid_projects", "Projects must be valid JSON describing explicit confirmed records.")


def apply_plan(root, plan, confirmed, started):
    if not re.fullmatch(r"[0-9a-f]{64}", confirmed) or plan.get("plan_digest") != confirmed or plan_digest(plan) != confirmed:
        fail("confirmation_mismatch", "The confirmed digest must match the unchanged displayed plan.")
    if plan.get("tool") != TOOL or plan.get("schema_version") not in (2, 3):
        fail("invalid_plan", "Unsupported project rules plan.")
    current = make_update_plan(root, plan.get("request")) if plan["schema_version"] == 3 else make_plan(root, plan.get("config"), plan.get("layout"))
    if current != plan:
        fail("plan_changed", "The root, existing files, templates, or plan changed; stop affected writes and present the updated plan in the final reply for text feedback.")
    if plan["blockers"]:
        fail("manual_merge_required", "Existing rules need an assistant-reviewed merge diff in the plan before a user-requested edit; this automatic plan will not write any files.")
    guard = RootGuard(root)
    if list(identity(guard.path.lstat())) != plan["root_identity"]:
        fail("root_changed", "The project root changed before writing.")
    completed = []
    for item in plan["files"]:
        name = item["path"]
        raw, before = read_item(guard, name)
        if before != item["before"]:
            fail("item_changed", "A rule document changed before writing: " + name)
        if item["action"] == "keep":
            continue
        data = item["content"].encode("utf-8")
        guard.check()
        if item["action"] == "create":
            with (guard.path / name).open("xb") as output:
                started.append(name)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
        else:
            flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(guard.path / name, flags)
            try:
                info = os.fstat(fd)
                if info.st_nlink != 1 or not stat.S_ISREG(info.st_mode) or list(identity(info)) != before["identity"]:
                    fail("item_changed", "Rule document changed before appending: " + name)
                with exclusive_file(fd, before["size"] + len(data)):
                    info = os.fstat(fd)
                    os.lseek(fd, 0, os.SEEK_SET)
                    with os.fdopen(fd, "r+b", closefd=False) as output:
                        existing = output.read()
                        current_info = guard.item(name, "file")
                        if current_info is None or identity(current_info) != identity(info) or info.st_nlink != 1 or info.st_mtime_ns != before["mtime_ns"] or digest(existing) != before["sha256"]:
                            fail("item_changed", "Rule document changed while acquiring the write lock: " + name)
                        guard.check()
                        output.seek(0, os.SEEK_SET if item["action"] == "replace" else os.SEEK_END)
                        started.append(name)
                        output.write(data)
                        if item["action"] == "replace":
                            output.truncate()
                        output.flush()
                        os.fsync(output.fileno())
            finally:
                os.close(fd)
        _, after = read_item(guard, name)
        if after is None or after["sha256"] != item["after_sha256"]:
            fail("verification_failed", "Rule document does not match the approved result: " + name)
        completed.append(name)
    return {"ok": True, "root": str(guard.path), "plan_digest": confirmed, "written": completed, "unchanged": [item["path"] for item in plan["files"] if item["action"] == "keep"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    planning = commands.add_parser("plan", help="Print a read-only exact JSON plan for the assistant's final reply.")
    planning.add_argument("root")
    planning.add_argument("--language", choices=("chinese", "english"), required=True)
    planning.add_argument("--name")
    planning.add_argument("--timezone", required=True, help="Explicit timezone label from the project rules or known session context; stored as metadata, not resolved against a timezone database.")
    planning.add_argument("--layout-file", help="Selected layout JSON file, or - for stdin. Required when PROJECT_RULES.md does not exist.")
    updating = commands.add_parser("update-plan", help="Print a read-only incremental project registry plan, preserving all unowned bytes.")
    updating.add_argument("root")
    updating.add_argument("--projects-file", required=True, help="Explicit confirmed project records JSON file, or - for stdin.")
    applying = commands.add_parser("apply", help="Apply the exact plan on an explicit user operation request; do not ask again.")
    applying.add_argument("root")
    applying.add_argument("--plan-file", required=True, help="JSON plan file, or - to read stdin without a project cache.")
    applying.add_argument("--confirmed-plan-digest", required=True)
    args = parser.parse_args()
    started = []
    try:
        if args.command == "plan":
            result = make_plan(args.root, {"name": args.name or Path(os.path.abspath(args.root)).name, "timezone": args.timezone, "language": args.language}, load_layout(args.layout_file) if args.layout_file else None)
        elif args.command == "update-plan":
            result = make_update_plan(args.root, load_projects(args.projects_file))
        else:
            result = apply_plan(args.root, load_plan(args.plan_file), args.confirmed_plan_digest, started)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (OperationError, OSError, UnicodeError, ValueError) as error:
        print(json.dumps({"ok": False, "code": getattr(error, "code", "io_error"), "message": str(error), "started_writes": started, "note": "If started_writes is nonempty, inspect the reported files before retrying; this command is not a multi-file transaction."}, ensure_ascii=True, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
