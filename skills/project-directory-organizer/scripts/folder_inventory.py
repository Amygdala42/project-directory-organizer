#!/usr/bin/env python3
"""Read-only, iterative folder inventory using metadata only (Python 3.9+).

No file bodies are opened and no report/cache is written. Counts describe only
listed entries, never disk usage. A changing tree is not a consistent snapshot;
checks protect normal use, not adversarial filesystem replacement during a scan.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import stat

from path_safety import RootGuard, OperationError, is_reparse


GENERATED_DIRECTORIES = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", ".cache"})


def utc_time(timestamp=None):
    value = datetime.now(timezone.utc) if timestamp is None else datetime.fromtimestamp(timestamp, timezone.utc)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def positive_integer(value):
    try:
        number = int(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("must be a positive integer")
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def inventory(root, max_depth=None, max_entries=10000, include_generated=False):
    """Return an inventory; per-entry traversal failures produce a partial report.

    Root validation errors raise OperationError. Directory depth starts at one for
    root children. Omission count measures unexpanded ranges, not missing entries.
    Entry ordering follows the filesystem and is not an ordering guarantee.
    """
    if type(max_entries) is not int or max_entries < 1 or (
        max_depth is not None and (type(max_depth) is not int or max_depth < 1)
    ):
        raise OperationError("arguments", "max_entries and any max_depth must be positive integers.")
    guard = RootGuard(root)
    report = {
        "schema_version": 1,
        "status": "inventoried",
        "root": str(guard.path),
        "started_utc": utc_time(),
        "complete": True,
        "root_children_status": "pending",
        "entries": [],
        "omissions": [],
        "errors": [],
        "limits": {"max_depth": max_depth, "max_entries": max_entries},
        "policy": {
            "include_hidden": True,
            "include_generated": include_generated,
            "generated_directory_names": sorted(GENERATED_DIRECTORIES),
            "follow_reparse_points": False,
            "read_file_contents": False,
            "writes_files": False,
            "modified_time_is_creation_time": False,
            "ordering": "filesystem order; no ordering guarantee",
        },
    }
    entries, omissions, errors = report["entries"], report["omissions"], report["errors"]
    # A queue avoids Python recursion limits. Each iterator is closed before the
    # next directory starts, keeping open handles bounded independently of depth.
    pending = deque([(guard.path, 0, None, guard.original)])

    def relative(path):
        return path.relative_to(guard.path).as_posix()

    def children_state(item, value):
        if item is None:
            report["root_children_status"] = value
        else:
            item["children_status"] = value

    def omission(path, reason):
        omissions.append({"path": path, "reason": reason, "unlisted_entries": None})

    def error_record(path, operation, error):
        errors.append({
            "path": path, "operation": operation,
            "code": error.code if isinstance(error, OperationError) else type(error).__name__,
            "message": str(error),
        })

    def stop_at_limit(item=None):
        if item is not None:
            children_state(item, "skipped_limit")
        for _, _, queued_item, _ in pending:
            children_state(queued_item, "skipped_limit")
        omission(".", "max_entries")
        pending.clear()

    while pending:
        if len(entries) >= max_entries:
            stop_at_limit()
            break
        directory, parent_depth, parent_item, expected_identity = pending.popleft()
        directory_relative = relative(directory)
        try:
            guard.check()
            directory_info = directory.lstat()
            if is_reparse(directory_info) or not stat.S_ISDIR(directory_info.st_mode):
                raise OperationError("directory_changed", "A queued directory became a link or non-directory; it was not traversed.")
            if (directory_info.st_dev, directory_info.st_ino) != expected_identity:
                raise OperationError("directory_changed", "A queued directory was replaced; it was not traversed.")
            limit_reached = False
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    if len(entries) >= max_entries:
                        children_state(parent_item, "skipped_limit")
                        stop_at_limit()
                        limit_reached = True
                        break
                    path = directory / entry.name
                    name = relative(path)
                    depth = parent_depth + 1
                    item = {"path": name, "depth": depth}
                    entries.append(item)
                    try:
                        # On Windows DirEntry's cached stat may report inode 0;
                        # use lstat consistently for later directory identity checks.
                        info = path.lstat()
                        if is_reparse(info):
                            item.update(type="reparse_point", children_status="not_followed")
                            omission(name, "reparse_point")
                        elif stat.S_ISDIR(info.st_mode):
                            item["type"] = "directory"
                            if not include_generated and entry.name.casefold() in GENERATED_DIRECTORIES:
                                item["children_status"] = "skipped_generated"
                                omission(name, "generated_directory")
                            elif max_depth is not None and depth >= max_depth:
                                item["children_status"] = "skipped_depth"
                                omission(name, "max_depth")
                            else:
                                item["children_status"] = "pending"
                                pending.append((path, depth, item, (info.st_dev, info.st_ino)))
                        elif stat.S_ISREG(info.st_mode):
                            item.update(type="file", size_bytes=info.st_size)
                            try:
                                item["modified_utc"] = utc_time(info.st_mtime)
                            except (ValueError, OverflowError, OSError) as error:
                                item["modified_utc"] = None
                                error_record(name, "modified_time", error)
                        else:
                            item["type"] = "other"
                    except OSError as error:
                        item["type"] = "unavailable"
                        error_record(name, "stat", error)
            if not limit_reached:
                children_state(parent_item, "enumerated")
        except (OSError, OperationError) as error:
            children_state(parent_item, "error")
            error_record(directory_relative, "enumerate", error)

    report["complete"] = not omissions and not errors
    report["finished_utc"] = utc_time()
    files = [item for item in entries if item["type"] == "file"]
    report["summary"] = {
        "entries": len(entries),
        "files": len(files),
        "directories": sum(item["type"] == "directory" for item in entries),
        "reparse_points": sum(item["type"] == "reparse_point" for item in entries),
        "other_or_unavailable": sum(item["type"] in {"other", "unavailable"} for item in entries),
        "file_bytes": sum(item["size_bytes"] for item in files),
        "count_scope": "listed entries only; file_bytes sums listed regular-file lengths, not disk usage",
        "omission_ranges": len(omissions),
        "errors": len(errors),
        "unlisted_entries": None if not report["complete"] else 0,
    }
    return report


def summarize(report, top=10):
    """Render listed metadata without entries, I/O, or changes to the report.

    Original scan state and totals remain intact. Aggregations are exhaustive
    over listed entries; only file rankings are limited by top. Ages use the
    scan's finished_utc, never the time at which this renderer is called.
    """
    if type(top) is not int or top < 1:
        raise OperationError("arguments", "top must be a positive integer.")
    result = deepcopy({key: value for key, value in report.items() if key != "entries"})
    summary = result["summary"]

    def parsed_time(value):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None

    reference = parsed_time(report.get("finished_utc"))
    buckets = {age: {"age": age, "files": 0, "file_bytes": 0}
               for age in ("<7d", "7-30d", "30-90d", "90-365d", ">=365d", "future", "unknown")}
    groups, extensions, files, recent = {}, {}, [], []
    for item in report["entries"]:
        path, kind = item["path"], item["type"]
        group_path = path.split("/", 1)[0] if "/" in path or kind == "directory" else "."
        group = groups.setdefault(group_path, {
            "path": group_path, "entries": 0, "files": 0, "directories": 0,
            "reparse_points": 0, "other_or_unavailable": 0, "file_bytes": 0,
        })
        group["entries"] += 1
        category = {"file": "files", "directory": "directories", "reparse_point": "reparse_points"}.get(
            kind, "other_or_unavailable")
        group[category] += 1
        if kind != "file":
            continue
        size = item["size_bytes"]
        group["file_bytes"] += size
        extension = PurePosixPath(path).suffix.casefold()
        extension_group = extensions.setdefault(extension, {"extension": extension, "files": 0, "file_bytes": 0})
        extension_group["files"] += 1
        extension_group["file_bytes"] += size
        file_item = {"path": path, "size_bytes": size, "modified_utc": item.get("modified_utc")}
        files.append(file_item)
        modified = parsed_time(item.get("modified_utc"))
        age = "unknown"
        if reference is not None and modified is not None:
            seconds = (reference - modified).total_seconds()
            if seconds < 0:
                age = "future"
            else:
                age = ">=365d"
                for days, label in ((7, "<7d"), (30, "7-30d"), (90, "30-90d"), (365, "90-365d")):
                    if seconds < days * 86400:
                        age = label
                        break
                recent.append((seconds, file_item))
        buckets[age]["files"] += 1
        buckets[age]["file_bytes"] += size
    summary.update(
        top=top,
        reference_utc=report.get("finished_utc") if reference is not None else None,
        by_top_level=sorted(groups.values(), key=lambda item: (-item["file_bytes"], item["path"])),
        by_extension=sorted(extensions.values(), key=lambda item: (-item["file_bytes"], item["extension"])),
        by_modified_age=list(buckets.values()),
        largest_files=sorted(files, key=lambda item: (-item["size_bytes"], item["path"]))[:top],
        recently_modified_files=[item for _, item in sorted(recent, key=lambda pair: (pair[0], pair[1]["path"]))[:top]],
    )
    return result


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise OperationError("arguments", message)


def main(argv=None):
    parser = JsonArgumentParser(description=__doc__)
    parser.add_argument("root", help="Existing directory; root/ancestor links and reparse points are rejected")
    parser.add_argument("--max-depth", type=positive_integer, default=None,
                        help="Root children are depth 1; omitted means no depth limit")
    parser.add_argument("--max-entries", type=positive_integer, default=10000,
                        help="Maximum number of listed entries (default: 10000)")
    parser.add_argument("--include-generated", action="store_true",
                        help="Also expand common dependency/cache directories; links are still never followed")
    parser.add_argument("--summary", action="store_true",
                        help="Omit detailed entries and add metadata summaries; preserve all scan state")
    parser.add_argument("--top", type=positive_integer, default=10,
                        help="Number of files in each summary ranking (default: 10); does not change scanning")
    try:
        args = parser.parse_args(argv)
        result = inventory(args.root, args.max_depth, args.max_entries, args.include_generated)
        if args.summary:
            result = summarize(result, args.top)
        exit_code = 0
    except OperationError as error:
        result, exit_code = {"status": "error", "code": error.code, "message": str(error)}, 1
    except OSError as error:
        result, exit_code = {"status": "error", "code": "filesystem_error", "message": str(error)}, 1
    # NTFS permits unpaired UTF-16 surrogates in names. JSON escapes preserve
    # those paths without making the UTF-8 stdout encoding fail for the report.
    print(json.dumps(result, ensure_ascii=True))
    return exit_code


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
