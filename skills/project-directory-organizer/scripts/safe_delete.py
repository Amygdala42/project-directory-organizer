#!/usr/bin/env python3
"""Plan exact deletions; quarantine first, restore, or explicitly purge a batch.

Python 3.9+ standard library. Mutations are supported on Windows only; other
platforms can create read-only plans. This tool validates scope, not permission.
Normal concurrent operations are locked. Malicious filesystem replacement and
power loss are not transactional: retain the batch journal for manual recovery.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import sys
import uuid

sys.dont_write_bytecode = True
from path_safety import RootGuard, OperationError, fail, identity, is_reparse


TOOL = "codex-project-setup.safe-delete"
TRASH = ".project-trash"
LOCK = ".project-delete.lock"
MAX_RECORD_BYTES = 64 * 1024 * 1024
PLAN_KEYS_V1 = {"schema_version", "tool", "created_utc", "root", "root_identity", "allow_library", "max_entries", "targets"}
PLAN_KEYS = (PLAN_KEYS_V1 - {"allow_library"}) | {"originals", "allow_originals"}
ENTRY_KEYS = {"path", "kind", "identity", "size", "mtime_ns", "sha256"}
MANIFEST_KEYS = {"schema_version", "tool", "batch", "created_utc", "updated_utc", "status", "plan", "progress", "error"}
STATUSES = {"applying", "quarantined", "restoring", "restored", "purging", "purged", "apply_failed", "restore_failed", "purge_failed"}
PROGRESS = {"pending", "moving", "stored", "restoring", "restored", "purging", "purged"}
RESERVED = {"con", "prn", "aux", "nul", *("com" + str(n) for n in range(1, 10)), *("lpt" + str(n) for n in range(1, 10))}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_bytes(value):
    # ASCII JSON preserves NTFS names containing unpaired UTF-16 surrogates.
    raw = (json.dumps(value, ensure_ascii=True, indent=2) + "\n").encode("utf-8")
    if len(raw) > MAX_RECORD_BYTES:
        fail("invalid_json", "The record exceeds the supported JSON byte limit.")
    return raw


def literal_path(value, empty=False):
    """Portable literal relative path; never interpret glob or shell syntax."""
    if empty and value == "":
        return ""
    if not isinstance(value, str) or not value or PureWindowsPath(value).drive or value.startswith(("/", "\\")):
        fail("unsafe_path", "Use a nonempty literal path relative to the selected root.")
    value = value.replace("\\", "/")
    parts = value.split("/")
    for part in parts:
        if part in {"", ".", ".."} or part.endswith((".", " ")) or part.split(".")[0].casefold() in RESERVED:
            fail("unsafe_path", "Empty, parent, reserved, or ambiguous path components are not accepted.")
        if any(ord(c) < 32 or c in ':*?<>|"' for c in part):
            fail("unsafe_path", "Wildcards, control characters, streams, and ambiguous filenames are not accepted.")
    return "/".join(parts)


def selected_path(value, allow_originals, originals=None):
    value = literal_path(value)
    parts = {part.casefold() for part in value.split("/")}
    if parts.intersection({TRASH.casefold(), LOCK.casefold()}):
        fail("protected_path", "The deletion journal, lock, and quarantine cannot be selected for ordinary deletion.")
    if not allow_originals:
        if originals is None and "library" in parts:
            fail("protected_library", "This legacy plan requires explicit original-material authorization.")
        if originals is not None:
            key = value.casefold()
            for original in originals:
                protected = original.casefold()
                if key == protected or key.startswith(protected + "/") or protected.startswith(key + "/"):
                    fail("protected_originals", "Selected targets overlap declared original materials; include --allow-originals only when explicitly authorized for these exact targets.")
    return value


def long_existing_path(path):
    """Expand Windows 8.3 names only after the caller's no-link checks.

    This is name expansion, not Path.resolve(): root/ancestor/target reparse
    points must already have been rejected by RootGuard or safe_path.
    """
    if os.name != "nt":
        return path
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_long = kernel.GetLongPathNameW
    get_long.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    get_long.restype = wintypes.DWORD
    capacity = 32768
    while True:
        buffer = ctypes.create_unicode_buffer(capacity)
        length = get_long(str(path), buffer, capacity)
        if not length:
            raise ctypes.WinError(ctypes.get_last_error())
        if length < capacity:
            return Path(buffer.value)
        capacity = length + 1


def validate_root_scope(guard, allow_originals, originals=None):
    guard.check()
    parts = {part.casefold() for part in long_existing_path(guard.path).parts}
    if TRASH.casefold() in parts:
        fail("protected_path", "A quarantine or its descendant cannot be rebased as the deletion root.")
    if originals is None and "library" in parts and not allow_originals:
        fail("protected_library", "A root inside a legacy library requires explicit original-material authorization in its plan.")


def validate_mutation_scope(guard, plan):
    """Preserve old journal-location restrictions without inferring new roles."""
    if plan["schema_version"] == 1 and any(part.casefold() == "library" for part in long_existing_path(guard.path).parts):
        fail("library_root", "A legacy deletion journal cannot be written inside its protected library root; retain the old journal and use its original project root.")


def safe_path(guard, value, missing=False):
    """Check every component; never resolve through links or reparse points."""
    value = literal_path(value)
    guard.check()
    path = guard.path
    parts = value.split("/")
    for index, part in enumerate(parts):
        path = path / part
        try:
            info = path.lstat()
        except FileNotFoundError:
            if missing and index == len(parts) - 1:
                return path, None
            fail("missing_path", "A required path or parent is missing: " + value)
        if is_reparse(info):
            fail("reparse_point", "Symlinks, junctions, and reparse points are refused: " + value)
        if info.st_dev != guard.original[0]:
            fail("different_volume", "Every selected path must remain on the selected root's volume.")
        if index != len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            fail("invalid_parent", "A path parent is not a directory: " + value)
    return path, info


def canonical_selected_path(guard, value, allow_originals, missing=False, originals=None):
    """Check literal scope and physical components before expanding aliases."""
    value = selected_path(value, allow_originals, originals)
    path, info = safe_path(guard, value, missing=missing)
    # A restore destination may not exist; its existing parent still must not
    # hide a protected directory behind an 8.3 name.
    expanded = long_existing_path(path) if info is not None else long_existing_path(path.parent) / path.name
    try:
        relative = expanded.relative_to(long_existing_path(guard.path)).as_posix()
    except ValueError:
        fail("unsafe_path", "The expanded path is outside the selected root.")
    return selected_path(relative, allow_originals, originals)


def validate_originals(values):
    if not isinstance(values, list) or len(values) > 100000:
        fail("invalid_plan", "originals must list at most 100000 exact project-relative paths.")
    for value in values:
        if selected_path(value, True, []) != value:
            fail("invalid_plan", "Original-material paths must use canonical forward-slash separators.")
    return values


def canonical_originals(guard, values):
    """Normalize known originals before a new plan; recovery uses stored paths."""
    if not isinstance(values, list) or len(values) > 100000:
        fail("invalid_plan", "originals must list at most 100000 exact project-relative paths.")
    normalized = [selected_path(value, True, []) for value in values]
    validate_originals(normalized)
    result, seen = [], set()
    for value in normalized:
        value = canonical_selected_path(guard, value, True, originals=[])
        if value.casefold() not in seen:
            result.append(value)
            seen.add(value.casefold())
    return result


def plan_policy(plan):
    return (plan["allow_library"], None) if plan["schema_version"] == 1 else (plan["allow_originals"], plan["originals"])


def metadata(info):
    return (identity(info), info.st_size, info.st_mtime_ns, stat.S_IFMT(info.st_mode))


def fingerprint(guard, full, relative):
    path, info = safe_path(guard, full)
    if stat.S_ISDIR(info.st_mode):
        kind, digest = "directory", None
    elif stat.S_ISREG(info.st_mode):
        kind = "file"
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            if metadata(os.fstat(handle.fileno())) != metadata(info):
                fail("changed", "A file changed while it was being opened: " + full)
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        _, fresh = safe_path(guard, full)
        if metadata(fresh) != metadata(info):
            fail("changed", "A file changed during fingerprinting: " + full)
        digest = hasher.hexdigest()
    else:
        fail("unsupported_item", "Only regular files and directories can be selected: " + full)
    return {"path": relative, "kind": kind, "identity": list(identity(info)), "size": info.st_size, "mtime_ns": info.st_mtime_ns, "sha256": digest}


def snapshot(guard, value, budget, allow_originals=False, internal=False, originals=None):
    if not internal:
        value = canonical_selected_path(guard, value, allow_originals, originals=originals)
    result = []
    stack = [(value, "", False, None)]
    while stack:
        full, relative, finishing, previous = stack.pop()
        if finishing:
            if fingerprint(guard, full, relative) != previous:
                fail("changed", "A directory changed during traversal: " + full)
            continue
        if relative and not internal:
            selected_path(full, allow_originals, originals)
        budget[0] -= 1
        if budget[0] < 0:
            fail("entry_limit", "Traversal exceeds --max-entries; no executable truncated plan was produced.")
        record = fingerprint(guard, full, relative)
        result.append(record)
        if record["kind"] == "directory":
            path, _ = safe_path(guard, full)
            children = sorted(p.name for p in path.iterdir())
            stack.append((full, relative, True, record))
            for name in reversed(children):
                child = name if not relative else relative + "/" + name
                literal_path(child)
                stack.append((full + "/" + name, child, False, None))
    return sorted(result, key=lambda entry: entry["path"])


def unique_targets(paths):
    seen = []
    for path in paths:
        folded = path.casefold()
        if any(folded == existing or folded.startswith(existing + "/") or existing.startswith(folded + "/") for existing in seen):
            fail("overlap", "Targets must be distinct and must not contain one another.")
        seen.append(folded)


def valid_identity(value):
    return isinstance(value, list) and len(value) == 2 and all(type(item) is int and item >= 0 for item in value)


def validate_plan(guard, plan):
    if not isinstance(plan, dict) or type(plan.get("schema_version")) is not int or plan["schema_version"] not in (1, 2):
        fail("invalid_plan", "Unsupported deletion-plan schema.")
    expected_keys = PLAN_KEYS_V1 if plan["schema_version"] == 1 else PLAN_KEYS
    if set(plan) != expected_keys or plan["tool"] != TOOL:
        fail("invalid_plan", "Unsupported deletion-plan schema.")
    if not isinstance(plan["root"], str) or os.path.normcase(plan["root"]) != os.path.normcase(str(guard.path)) or not valid_identity(plan["root_identity"]) or tuple(plan["root_identity"]) != guard.original:
        fail("wrong_root", "The plan belongs to a different root path or filesystem directory.")
    allow_originals, originals = plan_policy(plan)
    if not isinstance(plan["created_utc"], str) or type(allow_originals) is not bool or type(plan["max_entries"]) is not int or not 1 <= plan["max_entries"] <= 100000:
        fail("invalid_plan", "Invalid plan settings.")
    if plan["schema_version"] == 2:
        validate_originals(originals)
    validate_root_scope(guard, allow_originals, originals)
    if not isinstance(plan["targets"], list) or not plan["targets"]:
        fail("invalid_plan", "A plan must contain at least one exact target.")
    count = 0
    paths = []
    for target in plan["targets"]:
        if not isinstance(target, dict) or set(target) != {"path", "entries"}:
            fail("invalid_plan", "Invalid target record.")
        value = selected_path(target["path"], allow_originals, originals)
        if value != target["path"]:
            fail("invalid_plan", "Plan paths must use canonical forward-slash separators.")
        paths.append(value)
        entries = target["entries"]
        if not isinstance(entries, list) or not entries:
            fail("invalid_plan", "Every target requires a complete recorded tree.")
        names = []
        for entry in entries:
            count += 1
            if count > plan["max_entries"] or not isinstance(entry, dict) or set(entry) != ENTRY_KEYS:
                fail("invalid_plan", "Invalid or excessive tree entries.")
            relative = literal_path(entry["path"], empty=True)
            if relative:
                selected_path(value + "/" + relative, allow_originals, originals)
            if relative != entry["path"] or entry["kind"] not in {"file", "directory"} or not valid_identity(entry["identity"]):
                fail("invalid_plan", "Invalid tree path, kind, or identity.")
            if type(entry["size"]) is not int or entry["size"] < 0 or type(entry["mtime_ns"]) is not int:
                fail("invalid_plan", "Invalid tree fingerprint.")
            if entry["kind"] == "directory":
                if entry["sha256"] is not None:
                    fail("invalid_plan", "Directories must not contain file digests.")
            elif not isinstance(entry["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
                fail("invalid_plan", "Invalid file digest.")
            names.append(relative)
        if names != sorted(names) or names[0] != "" or len(set(name.casefold() for name in names)) != len(names):
            fail("invalid_plan", "Tree entries must include the selected root and unique sorted paths.")
    unique_targets(paths)
    return plan


def make_plan(guard, paths, allow_originals=False, maximum=10000, originals=()):
    if not 1 <= maximum <= 100000:
        fail("entry_limit", "--max-entries must be between 1 and 100000.")
    originals = canonical_originals(guard, list(originals))
    validate_root_scope(guard, allow_originals, originals)
    paths = [canonical_selected_path(guard, value, allow_originals, originals=originals) for value in paths]
    unique_targets(paths)
    budget = [maximum]
    targets = [{"path": value, "entries": snapshot(guard, value, budget, allow_originals, originals=originals)} for value in paths]
    plan = {"schema_version": 2, "tool": TOOL, "created_utc": now(), "root": str(guard.path), "root_identity": list(guard.original), "originals": originals, "allow_originals": allow_originals, "max_entries": maximum, "targets": targets}
    return validate_plan(guard, plan)


def verify_targets(guard, plan, quarantine=None):
    allow_originals, originals = plan_policy(plan)
    if quarantine is None:
        # Also validate saved/edited plans: string-only overlap checks cannot
        # detect a long name and an 8.3 alias of that same entry or its parent.
        if originals is not None and canonical_originals(guard, originals) != originals:
            fail("invalid_plan", "Original-material paths contain aliases or changed path names; create a fresh plan.")
        expanded = [canonical_selected_path(guard, target["path"], allow_originals, originals=originals) for target in plan["targets"]]
        unique_targets(expanded)
        if any(actual.casefold() != target["path"].casefold() for actual, target in zip(expanded, plan["targets"])):
            fail("invalid_plan", "The plan contains Windows short-name aliases; create a fresh plan using the expanded names.")
    budget = [plan["max_entries"]]
    for index, target in enumerate(plan["targets"]):
        source = target["path"] if quarantine is None else quarantine + "/items/" + format(index, "04d")
        if snapshot(guard, source, budget, allow_originals, internal=quarantine is not None, originals=originals) != target["entries"]:
            fail("changed", "A selected tree changed since its recorded plan: " + source)


def unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail("invalid_json", "Duplicate JSON keys are not accepted.")
        result[key] = value
    return result


def read_json(path):
    info = path.lstat()
    if is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECORD_BYTES:
        fail("invalid_json", "The record must be a regular file of at most 64 MiB.")
    raw = path.read_bytes()
    try:
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_json), raw
    except (ValueError, UnicodeError, RecursionError):
        fail("invalid_json", "The record must contain valid UTF-8 JSON.")


def write_new(guard, value, data):
    path, info = safe_path(guard, value, missing=True)
    if info is not None:
        fail("collision", "Refusing to overwrite an existing path: " + value)
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


@contextmanager
def locked(guard):
    token = json_bytes({"tool": TOOL, "pid": os.getpid(), "token": uuid.uuid4().hex})
    try:
        write_new(guard, LOCK, token)
    except (FileExistsError, OperationError) as error:
        if isinstance(error, FileExistsError) or error.code == "collision":
            fail("locked", "Another deletion operation owns the lock. Inspect its owner and journal; do not remove it blindly.")
        raise
    try:
        yield
    finally:
        try:
            path, info = safe_path(guard, LOCK)
            if stat.S_ISREG(info.st_mode) and path.read_bytes() == token:
                path.unlink()
        except (OSError, OperationError):
            pass


def check_trash(guard, create=False):
    path, info = safe_path(guard, TRASH, missing=True)
    owner = {"schema_version": 1, "tool": TOOL, "root": str(guard.path), "root_identity": list(guard.original)}
    if info is None:
        if not create:
            return False
        guard.check()
        path.mkdir()
        write_new(guard, TRASH + "/owner.json", json_bytes(owner))
    else:
        if not stat.S_ISDIR(info.st_mode):
            fail("invalid_trash", "The reserved quarantine path is not a directory.")
        owner_path, _ = safe_path(guard, TRASH + "/owner.json")
        actual, _ = read_json(owner_path)
        # Match validate_plan's root spelling rules without rewriting old owners.
        if (not isinstance(actual, dict) or not isinstance(actual.get("root"), str)
                or dict(actual, root=os.path.normcase(actual["root"]))
                != dict(owner, root=os.path.normcase(owner["root"]))):
            fail("invalid_trash", "The existing quarantine belongs to an unknown root or tool.")
    return True


def preflight_manifest(manifest):
    # Reserve the longest normal status and per-target progress labels before
    # any persistent change. Also budget the next generated update timestamp:
    # existing journals may use a shorter timestamp or compact JSON encoding.
    json_bytes(dict(manifest, updated_utc=now(), status="quarantined", progress=["restoring"] * len(manifest["progress"])))


def save_manifest(guard, relative, manifest, previous):
    manifest["updated_utc"] = now()
    raw = json_bytes(manifest)
    path, info = safe_path(guard, relative)
    if not stat.S_ISREG(info.st_mode) or path.read_bytes() != previous:
        fail("journal_changed", "The batch journal changed during this operation.")
    temporary = relative.rsplit("/", 1)[0] + "/.manifest-" + uuid.uuid4().hex + ".tmp"
    write_new(guard, temporary, raw)
    temporary_path, _ = safe_path(guard, temporary)
    try:
        path, info = safe_path(guard, relative)
        if not stat.S_ISREG(info.st_mode) or path.read_bytes() != previous:
            fail("journal_changed", "The batch journal changed before its progress update.")
        os.replace(temporary_path, path)
    finally:
        try:
            pending, pending_info = safe_path(guard, temporary, missing=True)
            if pending_info is not None and stat.S_ISREG(pending_info.st_mode) and pending.read_bytes() == raw:
                pending.unlink()
        except (OSError, OperationError):
            pass
    return raw


def move_exact(guard, source, destination):
    source_path, _ = safe_path(guard, source)
    destination_path, info = safe_path(guard, destination, missing=True)
    if info is not None:
        fail("collision", "Refusing to overwrite a destination: " + destination)
    # The destination is a new private batch path (apply) or a preflighted
    # original path (restore). Lock protects cooperating callers. A malicious
    # concurrent path swap is outside this stdlib guarantee. Mutation commands
    # are Windows-only, where os.rename also rejects a destination created
    # between this check and the rename, instead of replacing it.
    guard.check()
    os.rename(source_path, destination_path)


def record_failure(guard, manifest_path, manifest, previous, phase, error):
    manifest["status"] = phase + "_failed"
    manifest["error"] = {"code": getattr(error, "code", "filesystem_error"), "message": str(error)}
    try:
        save_manifest(guard, manifest_path, manifest, previous)
    except (OSError, OperationError):
        pass
    raised = OperationError(getattr(error, "code", "filesystem_error"), str(error))
    raised.batch = manifest["batch"]
    raised.progress = manifest["progress"]
    raised.phase = phase
    raise raised from error


def initialization_failure(guard, plan, error, batch=None):
    raised = OperationError(getattr(error, "code", "filesystem_error"), str(error))
    raised.phase = "initialize"
    raised.progress = ["pending"] * len(plan["targets"])
    raised.not_moved = [target["path"] for target in plan["targets"]]
    raised.quarantine = str(guard.path / TRASH)
    if batch is not None:
        raised.batch = batch
        raised.manifest = str(guard.path / TRASH / batch / "manifest.json")
    raised.recovery = "No selected targets were moved. Inspect the reported quarantine and any partial records manually before retrying."
    raise raised from error


def apply_plan(guard, plan):
    validate_plan(guard, plan)
    validate_mutation_scope(guard, plan)
    allow_originals, originals = plan_policy(plan)
    verify_targets(guard, plan)
    batch = uuid.uuid4().hex
    manifest = {"schema_version": 1, "tool": TOOL, "batch": batch, "created_utc": now(), "updated_utc": now(), "status": "applying", "plan": plan, "progress": ["pending"] * len(plan["targets"]), "error": None}
    preflight_manifest(manifest)
    raw = json_bytes(manifest)
    check_trash(guard)
    with locked(guard):
        validate_plan(guard, plan)
        verify_targets(guard, plan)
        try:
            check_trash(guard, create=True)
        except (OSError, OperationError) as error:
            initialization_failure(guard, plan, error)
        relative = TRASH + "/" + batch
        manifest_path = relative + "/manifest.json"
        try:
            path, _ = safe_path(guard, relative, missing=True)
            path.mkdir()
            items, _ = safe_path(guard, relative + "/items", missing=True)
            items.mkdir()
            write_new(guard, manifest_path, raw)
        except (OSError, OperationError) as error:
            initialization_failure(guard, plan, error, batch)
        try:
            for index, target in enumerate(plan["targets"]):
                if snapshot(guard, target["path"], [plan["max_entries"]], allow_originals, originals=originals) != target["entries"]:
                    fail("changed", "A target changed before its move: " + target["path"])
                manifest["progress"][index] = "moving"
                raw = save_manifest(guard, manifest_path, manifest, raw)
                move_exact(guard, target["path"], relative + "/items/" + format(index, "04d"))
                manifest["progress"][index] = "stored"
                raw = save_manifest(guard, manifest_path, manifest, raw)
            verify_targets(guard, plan, relative)
            manifest["status"] = "quarantined"
            save_manifest(guard, manifest_path, manifest, raw)
        except (OSError, OperationError) as error:
            record_failure(guard, manifest_path, manifest, raw, "apply", error)
    return {"status": "quarantined", "root": str(guard.path), "batch": batch, "targets": [target["path"] for target in plan["targets"]], "manifest": str(guard.path / manifest_path), "recoverable": True}


def load_batch(guard, batch):
    if not isinstance(batch, str) or not re.fullmatch(r"[0-9a-f]{32}", batch):
        fail("invalid_batch", "Use the exact 32-character batch ID returned by apply.")
    check_trash(guard)
    relative = TRASH + "/" + batch
    directory, info = safe_path(guard, relative)
    if not stat.S_ISDIR(info.st_mode) or {p.name for p in directory.iterdir()} != {"items", "manifest.json"}:
        fail("invalid_batch", "The batch contains missing or unknown top-level entries.")
    manifest_path, _ = safe_path(guard, relative + "/manifest.json")
    manifest, raw = read_json(manifest_path)
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_KEYS or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1 or manifest["tool"] != TOOL or manifest["batch"] != batch:
        fail("invalid_batch", "Unsupported or altered batch journal.")
    if not isinstance(manifest["status"], str) or manifest["status"] not in STATUSES or not isinstance(manifest["created_utc"], str) or not isinstance(manifest["updated_utc"], str):
        fail("invalid_batch", "Invalid journal status or timestamps.")
    validate_plan(guard, manifest["plan"])
    validate_mutation_scope(guard, manifest["plan"])
    progress = manifest["progress"]
    if not isinstance(progress, list) or len(progress) != len(manifest["plan"]["targets"]) or any(not isinstance(value, str) or value not in PROGRESS for value in progress):
        fail("invalid_batch", "Invalid per-target progress journal.")
    if manifest["status"] != "quarantined" or any(value != "stored" for value in progress) or manifest["error"] is not None:
        fail("incomplete_batch", "Only a complete, unchanged quarantined batch can be restored or purged automatically. Inspect any interrupted operation manually.")
    preflight_manifest(manifest)
    items, info = safe_path(guard, relative + "/items")
    if not stat.S_ISDIR(info.st_mode) or {p.name for p in items.iterdir()} != {format(index, "04d") for index in range(len(progress))}:
        fail("invalid_batch", "Quarantine items differ from the recorded target set.")
    verify_targets(guard, manifest["plan"], relative)
    return relative, manifest, raw


def preflight_restore(guard, manifest):
    allow_originals, originals = plan_policy(manifest["plan"])
    expanded = []
    for target in manifest["plan"]["targets"]:
        expanded.append(canonical_selected_path(guard, target["path"], allow_originals, missing=True, originals=originals))
        _, info = safe_path(guard, target["path"], missing=True)
        if info is not None:
            fail("collision", "The original location is occupied; no files were overwritten: " + target["path"])
    unique_targets(expanded)


def restore_batch(guard, batch):
    _, initial, _ = load_batch(guard, batch)
    preflight_restore(guard, initial)
    with locked(guard):
        relative, manifest, raw = load_batch(guard, batch)
        preflight_restore(guard, manifest)
        manifest_path = relative + "/manifest.json"
        manifest["status"] = "restoring"
        raw = save_manifest(guard, manifest_path, manifest, raw)
        try:
            for index, target in enumerate(manifest["plan"]["targets"]):
                source = relative + "/items/" + format(index, "04d")
                if snapshot(guard, source, [manifest["plan"]["max_entries"]], internal=True) != target["entries"]:
                    fail("changed", "A quarantine item changed before restoration.")
                manifest["progress"][index] = "restoring"
                raw = save_manifest(guard, manifest_path, manifest, raw)
                move_exact(guard, source, target["path"])
                manifest["progress"][index] = "restored"
                raw = save_manifest(guard, manifest_path, manifest, raw)
            manifest["status"] = "restored"
            save_manifest(guard, manifest_path, manifest, raw)
        except (OSError, OperationError) as error:
            record_failure(guard, manifest_path, manifest, raw, "restore", error)
    return {"status": "restored", "root": str(guard.path), "batch": batch, "targets": [target["path"] for target in manifest["plan"]["targets"]]}


def purge_entry(guard, source, entry):
    full = source + ("/" + entry["path"] if entry["path"] else "")
    path, info = safe_path(guard, full)
    if list(identity(info)) != entry["identity"]:
        fail("changed", "A quarantine entry was replaced before permanent deletion.")
    if entry["kind"] == "directory":
        if not stat.S_ISDIR(info.st_mode) or any(path.iterdir()):
            fail("changed", "A directory contains unplanned entries; permanent deletion stopped.")
        path.rmdir()
    else:
        if fingerprint(guard, full, entry["path"]) != entry:
            fail("changed", "A file changed before permanent deletion.")
        # Recheck the exact resolved boundary immediately before unlinking.
        path, _ = safe_path(guard, full)
        path.unlink()


def purge_batch(guard, batch, permanent):
    if not permanent:
        fail("permanent_required", "Permanent purge requires --permanent and explicit user authorization for this batch.")
    load_batch(guard, batch)
    with locked(guard):
        relative, manifest, raw = load_batch(guard, batch)
        manifest_path = relative + "/manifest.json"
        manifest["status"] = "purging"
        raw = save_manifest(guard, manifest_path, manifest, raw)
        try:
            for index, target in enumerate(manifest["plan"]["targets"]):
                source = relative + "/items/" + format(index, "04d")
                if snapshot(guard, source, [manifest["plan"]["max_entries"]], internal=True) != target["entries"]:
                    fail("changed", "A quarantine item changed before permanent deletion.")
                manifest["progress"][index] = "purging"
                raw = save_manifest(guard, manifest_path, manifest, raw)
                ordered = sorted(target["entries"], key=lambda entry: (entry["path"].count("/") + bool(entry["path"]), entry["path"]), reverse=True)
                for entry in ordered:
                    purge_entry(guard, source, entry)
                manifest["progress"][index] = "purged"
                raw = save_manifest(guard, manifest_path, manifest, raw)
            manifest["status"] = "purged"
            save_manifest(guard, manifest_path, manifest, raw)
        except (OSError, OperationError) as error:
            record_failure(guard, manifest_path, manifest, raw, "purge", error)
    return {"status": "purged", "root": str(guard.path), "batch": batch, "recoverable": False, "manifest": str(guard.path / manifest_path)}


class JsonParser(argparse.ArgumentParser):
    def error(self, message):
        fail("arguments", message)


def main(argv=None):
    parser = JsonParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=JsonParser)
    for name in ("plan", "apply", "restore", "purge"):
        command = commands.add_parser(name)
        command.add_argument("root", help="Existing project root; never a symlink or junction")
        if name == "plan":
            command.add_argument("--path", action="append", required=True, help="Exact relative file/directory; repeat for multiple targets")
            command.add_argument("--original-path", action="append", default=[], help="Known protected original file/directory, relative to the confirmed project root; repeat for each reviewed location")
            command.add_argument("--allow-originals", action="store_true", help="Use only for explicit authorization to delete the exact selected original-material targets")
            command.add_argument("--max-entries", type=int, default=10000)
        elif name == "apply":
            command.add_argument("--plan-file", required=True)
        else:
            command.add_argument("--batch", required=True)
            if name == "purge":
                command.add_argument("--permanent", action="store_true")
    try:
        args = parser.parse_args(argv)
        if args.command != "plan" and sys.platform != "win32":
            fail("unsupported_platform", "Only Windows supports mutation commands in this version; other platforms may use read-only plan.")
        guard = RootGuard(args.root)
        if args.command == "plan":
            result = make_plan(guard, args.path, args.allow_originals, args.max_entries, args.original_path)
        elif args.command == "apply":
            plan, _ = read_json(Path(args.plan_file))
            result = apply_plan(guard, plan)
        elif args.command == "restore":
            result = restore_batch(guard, args.batch)
        else:
            result = purge_batch(guard, args.batch, args.permanent)
        code = 0
    except OperationError as error:
        result, code = {"status": "error", "code": error.code, "message": str(error)}, 1
        if hasattr(error, "phase"):
            result.update(progress=error.progress, phase=error.phase, recovery=getattr(error, "recovery", "Inspect the retained batch journal; automatic continuation is disabled for interrupted operations."))
            for name in ("batch", "quarantine", "manifest", "not_moved"):
                if hasattr(error, name):
                    result[name] = getattr(error, name)
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        result, code = {"status": "error", "code": "filesystem_or_record_error", "message": str(error)}, 1
    print(json.dumps(result, ensure_ascii=True))
    return code


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
