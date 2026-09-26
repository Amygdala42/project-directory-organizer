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
        fail("invalid_config", "Use a timezone label such as Asia/Shanghai or UTC.")
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
    if len(parts) == 1 and parts[0].casefold() in {"agents.md", "project_rules.md"}:
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
    if not isinstance(layout, dict) or set(layout) != {"research", "directories", "environment", "outputs", "originals"}:
        fail("invalid_layout", "Layout must contain research, directories, environment, outputs, and originals.")
    research = layout_text(layout["research"], "research")
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
            if field == "originals" and not any(key == item or key.startswith(item + "/") for item in selected):
                fail("invalid_layout", "Each original-material path must belong to a selected directory.")
            paths.append(path)
        lists[field] = paths
    for original in lists["originals"]:
        for output in lists["outputs"]:
            a, b = path_key(original), path_key(output)
            if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                fail("invalid_layout", "Original-material and output locations must not overlap or contain one another.")
    return {"research": research, "directories": directories, "environment": environment, **lists}


def markdown_text(value):
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_{}\[\]()|#])", r"\\\1", value)


def quoted_path(value):
    return "“" + markdown_text(value) + "”"


def layout_fields(layout, language):
    rows = layout["directories"]
    table = "| 相对目录 | 用途 |\n| --- | --- |\n" + "\n".join("| " + markdown_text(row["path"]) + " | " + markdown_text(row["purpose"]) + " |" for row in rows) if rows else "本次未选业务子目录。"
    environment = "当前未设置项目环境，新增按实际任务确定。" if layout["environment"] is None else "环境统一归主项目" + quoted_path(layout["environment"]) + "；目录预留不代表已安装，实际配置按用户任务进行。"
    outputs = ""
    if layout["outputs"]:
        locations = "、".join(quoted_path(path) for path in layout["outputs"])
        batch = "YYYYMMDD用途" if language == "chinese" else "YYYYMMDD-purpose"
        filename = "内容名_v01.ext" if language == "chinese" else "content-name_v01.ext"
        outputs = "\n".join((
            "## 成果归档", "",
            "- 成果位置：" + locations + "。",
            "- 在对应位置下用 `" + batch + "` 建批次，日期取实际产出或修订日；实际产出或明确预留时才建，不强制成对建立结果与报告批次。",
            "- 同日同次工作复用批次，不同用途分开。",
            "- 普通成果采用 `" + filename + "`，修订递增且跨日连续，不覆盖旧版本；同次 PPT/PDF 使用相同主名和版本，配套材料共址。",
            "- 跨日仅为新增或修订成果建立当日批次，不复制未变化材料；稳定维护的代码、数据和参考资料不随日期移动。",
        ))
    originals = "原件内容、名称、位置和包结构保持原样。"
    if layout["originals"]:
        originals = "原件位置：" + "、".join(quoted_path(path) for path in layout["originals"]) + "。" + originals
    return {"PROJECT_SCOPE": "本文件所在目录为主项目根目录，下列路径均相对此目录。当前研究范围：" + markdown_text(layout["research"]) + "。", "DIRECTORY_TABLE": table, "ENVIRONMENT_RULE": environment, "OUTPUT_RULES": outputs, "ORIGINAL_RULES": originals}


def templates(config, layout, include_rules):
    values = {
        "PROJECT_NAME": config["name"], "TIMEZONE": config["timezone"],
        "LANGUAGE": "中文" if config["language"] == "chinese" else "English",
        "RULES_FILE": "PROJECT_RULES.md",
        "NAMING_RULE": "自建业务目录和文档用清楚、简短的中文名称。" if config["language"] == "chinese" else "自建业务目录用小写 ASCII 英文和连字符，文档用简短英文名。",
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


def apply_plan(root, plan, confirmed, started):
    if not re.fullmatch(r"[0-9a-f]{64}", confirmed) or plan.get("plan_digest") != confirmed or plan_digest(plan) != confirmed:
        fail("confirmation_mismatch", "The confirmed digest must match the unchanged displayed plan.")
    if plan.get("tool") != TOOL or plan.get("schema_version") != 2:
        fail("invalid_plan", "Unsupported project rules plan.")
    current = make_plan(root, plan.get("config"), plan.get("layout"))
    if current != plan:
        fail("plan_changed", "The root, existing files, templates, or plan changed; stop affected writes and present the updated plan in the final reply for text feedback.")
    if plan["blockers"]:
        fail("manual_merge_required", "Existing rules need an assistant-reviewed merge diff in the plan before a user-requested edit; this automatic plan will not write any files.")
    guard = RootGuard(root)
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
                        output.seek(0, os.SEEK_END)
                        started.append(name)
                        output.write(data)
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
    planning.add_argument("--timezone", default="Asia/Shanghai")
    planning.add_argument("--layout-file", help="Selected layout JSON file, or - for stdin. Required when PROJECT_RULES.md does not exist.")
    applying = commands.add_parser("apply", help="Apply the exact plan on an explicit user operation request; do not ask again.")
    applying.add_argument("root")
    applying.add_argument("--plan-file", required=True, help="JSON plan file, or - to read stdin without a project cache.")
    applying.add_argument("--confirmed-plan-digest", required=True)
    args = parser.parse_args()
    started = []
    try:
        if args.command == "plan":
            result = make_plan(args.root, {"name": args.name or Path(os.path.abspath(args.root)).name, "timezone": args.timezone, "language": args.language}, load_layout(args.layout_file) if args.layout_file else None)
        else:
            result = apply_plan(args.root, load_plan(args.plan_file), args.confirmed_plan_digest, started)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (OperationError, OSError, UnicodeError, ValueError) as error:
        print(json.dumps({"ok": False, "code": getattr(error, "code", "io_error"), "message": str(error), "started_writes": started, "note": "If started_writes is nonempty, inspect the reported files before retrying; this command is not a multi-file transaction."}, ensure_ascii=True, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
