#!/usr/bin/env python3
"""Execute reviewed, approved, independent moves; never infer reference safety.

Python 3.9+, standard library. plan and rollback-plan are read-only. Windows-only
mutations use no-overwrite rename, fingerprints and an explicit visible journal.
Confirmation digests bind exact plans, but do not establish user permission.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

sys.dont_write_bytecode = True
from path_safety import RootGuard, OperationError, fail, identity
from safe_delete import (TRASH, fingerprint, literal_path, long_existing_path, move_exact,
                         now, read_json, safe_path, unique_json, unique_targets)

TOOL = 'codex-project-directory.safe-move'
MAX_JOURNAL_BYTES = 64 * 1024 * 1024
PROTECTED_PARTS = {
    'library', 'reference', 'references', 'raw', '参考资料', '资料库', '原始数据', '原始资料',
    'code', 'src', 'scripts', 'notebooks', '代码', '源码', '脚本', '分析笔记本',
    'env', 'venv', 'environment', 'environments', 'node_modules', '环境',
    'config', '配置', 'agents.md', 'project_rules.md', 'readme.md', '目录规则.md',
    'package.json', 'package-lock.json', 'requirements.txt', 'pyproject.toml',
    'cargo.toml', 'cargo.lock', 'go.mod', 'go.sum', 'environment.yml',
}
CODE_SUFFIXES = {'.py', '.pyw', '.r', '.rmd', '.ipynb', '.js', '.jsx', '.ts', '.tsx',
                 '.sh', '.bat', '.cmd', '.ps1', '.exe', '.dll', '.so', '.c', '.cpp',
                 '.h', '.java', '.rs', '.jl', '.m', '.lock', '.sln', '.csproj'}
ITEM_KEYS = {'source', 'destination', 'risk', 'group', 'evidence'}
EVIDENCE_KEYS = {'scope', 'references', 'completeness', 'checked_paths'}
PLAN_KEYS = {'schema_version', 'tool', 'root', 'root_identity', 'created_utc', 'max_entries', 'items', 'assessment_inputs', 'log', 'log_parent_identity', 'plan_digest'}


def encoded(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def content_digest(plan):
    return digest({key: value for key, value in plan.items() if key != 'plan_digest'})


def protect(value):
    for component in Path(value).parts:
        name = component.casefold()
        if name.startswith('.') or name in PROTECTED_PARTS or Path(name).suffix in CODE_SUFFIXES:
            fail('protected_path', 'Originals, code, environments, rules and hidden/tool files cannot be moved by this command: ' + str(value))


def protect_project_root(guard):
    """Check the confirmed root itself, not unrelated hosting directory names."""
    guard.check()
    expanded = long_existing_path(guard.path)
    if any(part.casefold() == TRASH.casefold() for part in expanded.parts):
        fail('protected_path', 'A quarantine or its descendant cannot be used as a movement root.')
    # Keep root rebasing and 8.3-name protections. Relative object paths still
    # pass through protect(); only ancestors outside the project are exempt.
    protect(expanded.name)


def canonical(guard, value, missing=False):
    value = literal_path(value)
    path, info = safe_path(guard, value, missing=missing)
    full = long_existing_path(path) if info is not None else long_existing_path(path.parent) / path.name
    try:
        return full.relative_to(long_existing_path(guard.path)).as_posix(), info
    except ValueError:
        fail('unsafe_path', 'Path is outside the confirmed project root.')


def held_path(guard, value):
    """Normalize only accessible, no-link prefixes; never inspect held content."""
    parts = literal_path(value).split('/')
    prefix = ''
    guard.check()
    for index, part in enumerate(parts):
        candidate = prefix + ('/' if prefix else '') + part
        try:
            expanded, info = canonical(guard, candidate, missing=True)
        except OperationError as error:
            if error.code not in {'reparse_point', 'missing_path', 'invalid_parent'}:
                raise
            # Root/ancestor failures still block the entire plan. Only the
            # held object's own uninspectable suffix remains a suggestion.
            guard.check()
            return prefix + ('/' if prefix else '') + '/'.join(parts[index:])
        except OSError:
            guard.check()
            return prefix + ('/' if prefix else '') + '/'.join(parts[index:])
        if info is None:
            return expanded + ('/' + '/'.join(parts[index + 1:]) if index + 1 < len(parts) else '')
        prefix = expanded
    return prefix


def tree(guard, value, budget, moving=False):
    """Snapshot regular files/directories, with no link following or partial plan."""
    result = []
    pending = [(value, '')]
    while pending:
        full, relative = pending.pop()
        budget[0] -= 1
        if budget[0] < 0:
            fail('entry_limit', 'Complete review exceeds --max-entries; no partial executable plan is produced.')
        if moving:
            protect(full)
        entry = fingerprint(guard, full, relative)
        result.append(entry)
        if entry['kind'] == 'directory':
            directory, _ = safe_path(guard, full)
            for child in sorted(directory.iterdir(), reverse=True):
                name = literal_path(child.name)
                pending.append((full + '/' + name, name if not relative else relative + '/' + name))
    for entry in result:
        if entry['kind'] == 'directory':
            full = value + ('/' + entry['path'] if entry['path'] else '')
            if fingerprint(guard, full, entry['path']) != entry:
                fail('changed', 'A directory changed during snapshot: ' + full)
    return sorted(result, key=lambda record: record['path'])


def check_item(item):
    if not isinstance(item, dict) or set(item) != ITEM_KEYS:
        fail('invalid_mapping', 'Each item requires source, destination, risk, group and evidence.')
    for key in ('source', 'destination'):
        literal_path(item[key])
    if item['risk'] not in ('clear', 'risk', 'unknown') or not isinstance(item['group'], str) or not item['group'].strip():
        fail('invalid_mapping', 'Every item requires risk clear/risk/unknown and a nonempty dependency group.')
    evidence = item['evidence']
    if not isinstance(evidence, dict) or set(evidence) != EVIDENCE_KEYS:
        fail('incomplete_assessment', 'Evidence requires scope, references, completeness and checked_paths.')
    if not all(isinstance(evidence[key], str) for key in ('scope', 'references', 'completeness')):
        fail('incomplete_assessment', 'Assessment notes must be text.')
    if not isinstance(evidence['checked_paths'], list) or not all(isinstance(path, str) for path in evidence['checked_paths']):
        fail('incomplete_assessment', 'checked_paths must list reviewed paths relative to the root.')
    if item['risk'] == 'clear' and (not evidence['checked_paths'] or not all(evidence[key].strip() for key in ('scope', 'references', 'completeness'))):
        fail('incomplete_assessment', 'Clear requires complete scope, reference and group-completeness notes, and reviewed paths.')


def make_plan(guard, mapping, relative_log, maximum=10000):
    if not isinstance(mapping, dict) or set(mapping) != {'items'} or not isinstance(mapping['items'], list) or not mapping['items']:
        fail('invalid_mapping', 'Mapping must contain a nonempty items list only.')
    if type(maximum) is not int or not 1 <= maximum <= 100000:
        fail('entry_limit', '--max-entries must be 1 through 100000.')
    for item in mapping['items']:
        check_item(item)
    blocked_groups = {item['group'] for item in mapping['items'] if item['risk'] != 'clear'}
    budget = [maximum]
    result, active_paths, checked_paths = [], [], set()
    for item in mapping['items']:
        record = dict(item, evidence=dict(item['evidence']))
        record['decision'] = 'hold' if item['group'] in blocked_groups else 'move'
        record['entries'] = []
        if record['decision'] == 'hold':
            record.update(source=held_path(guard, item['source']), destination=held_path(guard, item['destination']))
        if record['decision'] == 'move':
            protect_project_root(guard)
            source, _ = canonical(guard, item['source'])
            destination, existing = canonical(guard, item['destination'], missing=True)
            protect(source)
            protect(destination)
            if existing is not None:
                fail('collision', 'Destination already exists: ' + destination)
            record.update(source=source, destination=destination)
            record['entries'] = tree(guard, source, budget, moving=True)
            active_paths.extend([source, destination])
            canonical_checks = sorted({canonical(guard, value)[0] for value in item['evidence']['checked_paths']})
            record['evidence']['checked_paths'] = canonical_checks
            checked_paths.update(canonical_checks)
        result.append(record)
    unique_targets(active_paths)
    # A held object nested in or overlapping any moving object remains held
    # regardless of an incorrectly assigned group. No partial dependency bundle.
    for record in result:
        if record['decision'] == 'hold':
            for name in ('source', 'destination'):
                held = literal_path(record[name])
                for active in active_paths:
                    a, b = active.casefold(), held.casefold()
                    if a == b or a.startswith(b + '/') or b.startswith(a + '/'):
                        fail('overlap', 'A held object overlaps a moving object; keep the related set together.')
    assessments = []
    for value in sorted(checked_paths):
        value, _ = canonical(guard, value)
        assessments.append({'path': value, 'entries': tree(guard, value, budget)})
    plan = {'schema_version': 1, 'tool': TOOL, 'root': str(guard.path), 'root_identity': list(guard.original),
            'created_utc': now(), 'max_entries': maximum, 'items': result, 'assessment_inputs': assessments}
    path, existing = journal_path(guard, relative_log, plan, missing=True)
    if existing is not None:
        fail('collision', 'Choose a new approved journal path; an existing log cannot be replaced.')
    plan['log'] = path.relative_to(guard.path).as_posix()
    plan['log_parent_identity'] = list(identity(path.parent.lstat()))
    plan['plan_digest'] = content_digest(plan)
    return plan


def validate(guard, plan):
    if not isinstance(plan, dict) or set(plan) != PLAN_KEYS or plan['schema_version'] != 1 or plan['tool'] != TOOL:
        fail('invalid_plan', 'Unsupported move plan.')
    if plan['plan_digest'] != content_digest(plan):
        fail('plan_digest', 'The plan changed; regenerate it and obtain approval for its new digest.')
    if not isinstance(plan['root'], str) or os.path.normcase(plan['root']) != os.path.normcase(str(guard.path)) or plan['root_identity'] != list(guard.original):
        fail('wrong_root', 'The plan belongs to a different project path or directory identity.')
    if not isinstance(plan['items'], list) or not plan['items'] or type(plan['max_entries']) is not int or not 1 <= plan['max_entries'] <= 100000:
        fail('invalid_plan', 'Invalid plan items or maximum.')
    for item in plan['items']:
        if not isinstance(item, dict) or set(item) != ITEM_KEYS | {'decision', 'entries'}:
            fail('invalid_plan', 'Invalid move item.')
        check_item({key: item[key] for key in ITEM_KEYS})
    blocked = {item['group'] for item in plan['items'] if item['risk'] != 'clear'}
    active = []
    for item in plan['items']:
        expected = 'hold' if item['group'] in blocked else 'move'
        if item['decision'] != expected or not isinstance(item['entries'], list):
            fail('invalid_plan', 'A risk or unknown group cannot be promoted to move.')
        if expected == 'move':
            protect_project_root(guard)
            protect(item['source'])
            protect(item['destination'])
            if not item['entries']:
                fail('invalid_plan', 'Every move needs its complete snapshot.')
            for entry in item['entries']:
                if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
                    fail('invalid_plan', 'Invalid fingerprint entry.')
                if entry['path']:
                    protect(literal_path(entry['path']))
            active.extend([item['source'], item['destination']])
    unique_targets(active)
    if not isinstance(plan['assessment_inputs'], list):
        fail('invalid_plan', 'Missing assessment fingerprints.')
    expected_checks = {path for item in plan['items'] if item['decision'] == 'move' for path in item['evidence']['checked_paths']}
    recorded_checks = []
    for record in plan['assessment_inputs']:
        if not isinstance(record, dict) or set(record) != {'path', 'entries'} or not isinstance(record['path'], str) or not isinstance(record['entries'], list):
            fail('invalid_plan', 'Invalid assessment snapshot.')
        literal_path(record['path'])
        recorded_checks.append(record['path'])
    if len(set(recorded_checks)) != len(recorded_checks) or set(recorded_checks) != expected_checks:
        fail('invalid_plan', 'Every reviewed input needs exactly one recorded snapshot.')
    for record in plan['items']:
        if record['decision'] == 'hold':
            for key in ('source', 'destination'):
                candidate = literal_path(record[key]).casefold()
                for moving in active:
                    moving = moving.casefold()
                    if candidate == moving or candidate.startswith(moving + '/') or moving.startswith(candidate + '/'):
                        fail('overlap', 'A held item overlaps the executable move set.')
    return [item for item in plan['items'] if item['decision'] == 'move']


def preflight(guard, plan, reverse=False):
    moves = validate(guard, plan)
    if not moves:
        fail('nothing_to_move', 'All objects are held; no move or successful-completion claim is allowed.')
    budget = [plan['max_entries']]
    for item in moves:
        source = item['destination'] if reverse else item['source']
        destination = item['source'] if reverse else item['destination']
        actual, _ = canonical(guard, source)
        target, existing = canonical(guard, destination, missing=True)
        protect(actual)
        protect(target)
        if existing is not None:
            fail('collision', 'Refusing to replace existing content: ' + destination)
        if tree(guard, actual, budget, moving=True) != item['entries']:
            fail('changed', 'Moved content changed; review before continuing: ' + source)
    if not reverse:
        for reviewed in plan['assessment_inputs']:
            if not isinstance(reviewed, dict) or set(reviewed) != {'path', 'entries'}:
                fail('invalid_plan', 'Invalid assessment fingerprint.')
            if tree(guard, reviewed['path'], budget) != reviewed['entries']:
                fail('assessment_changed', 'An assessment input changed; re-evaluate dependencies: ' + reviewed['path'])
    return moves


def journal_path(guard, relative, plan, missing=False):
    relative, info = canonical(guard, relative, missing=missing)
    if 'log' in plan and relative != plan['log']:
        fail('log_mismatch', 'The requested journal differs from the approved plan; regenerate the plan and obtain approval.')
    protect(relative)
    if not relative.lower().endswith('.jsonl'):
        fail('invalid_log', 'Choose a visible .jsonl journal in an existing approved records folder.')
    unique_targets([relative] + [item[key] for item in plan['items'] if item['decision'] == 'move' for key in ('source', 'destination')])
    if missing:
        # New plans/applies must not write into held suggestions. Keep the
        # recovery path for complete logs created by older versions.
        for item in plan['items']:
            if item['decision'] == 'hold':
                for key in ('source', 'destination'):
                    # Held suggestions may still overlap one another.
                    unique_targets([relative, item[key]])
    for reviewed in plan['assessment_inputs']:
        log, checked = relative.casefold(), reviewed['path'].casefold()
        if log == checked or log.startswith(checked + '/') or checked.startswith(log + '/'):
            fail('log_evidence_overlap', 'The journal must be outside reviewed evidence trees; choose another approved record path.')
    path, info = safe_path(guard, relative, missing=missing)
    if 'log_parent_identity' in plan and list(identity(path.parent.lstat())) != plan['log_parent_identity']:
        fail('log_parent_changed', 'The approved journal parent was replaced; regenerate the plan and obtain approval.')
    return path, info


@contextmanager
def journal_lock(path, create=False):
    """Lock the visible journal itself; no hidden lock, state or cache files."""
    import msvcrt
    with path.open('x+b' if create else 'r+b') as handle:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fail('locked', 'The journal is in use; do not run concurrent move/rollback commands.')
        try:
            yield handle
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def append(handle, event, previous):
    handle.seek(0)
    if handle.read() != previous:
        fail('journal_changed', 'The journal changed during execution; stop and inspect it.')
    raw = encoded(event)
    handle.seek(0, 2)
    handle.write(raw)
    handle.flush()
    os.fsync(handle.fileno())
    return previous + raw


def check_journal_capacity(plan, moves):
    """Reserve a complete successful apply and rollback before any writes."""
    size = len(encoded({'event': 'start', 'tool': TOOL, 'plan': plan}))
    for phase, status in (('apply', 'applied'), ('rollback', 'rolled_back')):
        for index in range(len(moves)):
            size += len(encoded({'event': phase + '_moving', 'index': index}))
            size += len(encoded({'event': phase + '_moved', 'index': index}))
        size += len(encoded({'event': status}))
    if size > MAX_JOURNAL_BYTES:
        fail('journal_limit', 'The complete move and rollback journal would exceed the readable size limit; review a smaller batch before execution.')


def mutate(guard, plan, moves, handle, previous, reverse=False):
    phase = 'rollback' if reverse else 'apply'
    try:
        for index, item in enumerate(moves):
            source = item['destination'] if reverse else item['source']
            destination = item['source'] if reverse else item['destination']
            # Recheck immediately before each individual rename.
            if tree(guard, source, [plan['max_entries']], moving=True) != item['entries']:
                fail('changed', 'A source changed before its move: ' + source)
            previous = append(handle, {'event': phase + '_moving', 'index': index}, previous)
            move_exact(guard, source, destination)
            if tree(guard, destination, [plan['max_entries']], moving=True) != item['entries']:
                fail('postcheck_failed', 'Post-move fingerprint differs; stop and inspect the visible journal.')
            previous = append(handle, {'event': phase + '_moved', 'index': index}, previous)
        status = 'rolled_back' if reverse else 'applied'
        append(handle, {'event': status}, previous)
        return {'status': status, 'moved_count': len(moves), 'held_count': len(plan['items']) - len(moves)}
    except (OSError, OperationError) as error:
        try:
            append(handle, {'event': phase + '_failed', 'error': str(error)}, previous)
        except (OSError, OperationError):
            pass
        raise


def apply(guard, plan, confirmation, relative_log):
    validate(guard, plan)
    if confirmation != plan['plan_digest']:
        fail('confirmation_mismatch', '--confirm must equal the exact approved plan_digest; a digest is not user consent.')
    moves = preflight(guard, plan)
    path, info = journal_path(guard, relative_log, plan, missing=True)
    if info is not None:
        fail('collision', 'The journal already exists; use a new approved path.')
    check_journal_capacity(plan, moves)
    if os.name != 'nt':
        fail('unsupported_platform', 'Mutations require Windows no-overwrite rename semantics.')
    with journal_lock(path, create=True) as handle:
        preflight(guard, plan)
        previous = append(handle, {'event': 'start', 'tool': TOOL, 'plan': plan}, b'')
        result = mutate(guard, plan, moves, handle, previous)
    return dict(result, log=relative_log, plan_digest=plan['plan_digest'])


def load_journal(guard, relative):
    path, info = safe_path(guard, relative)
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JOURNAL_BYTES:
        fail('invalid_log', 'Journal must be a regular file of at most 64 MiB.')
    raw = path.read_bytes()
    try:
        events = [json.loads(line, object_pairs_hook=unique_json) for line in raw.decode('utf-8').splitlines()]
    except (ValueError, UnicodeError, RecursionError):
        fail('invalid_log', 'Invalid journal; retain it for manual recovery.')
    if not events or not isinstance(events[0], dict) or set(events[0]) != {'event', 'tool', 'plan'} or events[0]['event'] != 'start' or events[0]['tool'] != TOOL:
        fail('invalid_log', 'Missing move journal header.')
    plan = events[0]['plan']
    moves = validate(guard, plan)
    expected = [events[0]]
    for index in range(len(moves)):
        expected.extend([{'event': 'apply_moving', 'index': index}, {'event': 'apply_moved', 'index': index}])
    expected.append({'event': 'applied'})
    if events != expected:
        fail('incomplete_log', 'Only a complete applied batch can be rolled back automatically; interrupted/changed batches need manual review.')
    journal_path(guard, relative, plan)
    return plan, raw


def rollback_plan(guard, relative):
    plan, raw = load_journal(guard, relative)
    moves = preflight(guard, plan, reverse=True)
    preview = {'tool': TOOL, 'root': str(guard.path), 'root_identity': list(guard.original), 'log': relative,
               'journal_sha256': hashlib.sha256(raw).hexdigest(), 'plan_digest': plan['plan_digest'],
               'moves': [{'source': item['destination'], 'destination': item['source']} for item in moves]}
    preview['rollback_digest'] = digest(preview)
    return preview


def rollback(guard, relative, confirmation):
    preview = rollback_plan(guard, relative)
    if confirmation != preview['rollback_digest']:
        fail('confirmation_mismatch', '--confirm must equal the exact approved rollback_digest.')
    plan, raw = load_journal(guard, relative)
    if os.name != 'nt':
        fail('unsupported_platform', 'Mutations require Windows no-overwrite rename semantics.')
    path, _ = journal_path(guard, relative, plan)
    with journal_lock(path) as handle:
        handle.seek(0)
        if handle.read() != raw:
            fail('journal_changed', 'The rollback preview changed; obtain fresh approval.')
        moves = preflight(guard, plan, reverse=True)
        result = mutate(guard, plan, moves, handle, raw, reverse=True)
    return dict(result, log=relative)


def read_mapping(value):
    if value != '-':
        return read_json(Path(value))[0]
    raw = sys.stdin.buffer.read(64 * 1024 * 1024 + 1)
    if len(raw) > 64 * 1024 * 1024:
        fail('invalid_json', 'Mapping exceeds 64 MiB.')
    try:
        return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique_json)
    except (ValueError, UnicodeError, RecursionError):
        fail('invalid_json', 'Mapping must be valid UTF-8 JSON.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('plan', 'apply', 'rollback-plan', 'rollback'):
        current = sub.add_parser(command)
        current.add_argument('root')
        if command == 'plan':
            current.add_argument('--mapping-file', required=True, help='UTF-8 JSON path or - for stdin')
            current.add_argument('--max-entries', type=int, default=10000)
        elif command == 'apply':
            current.add_argument('--plan-file', required=True)
        current.add_argument('--log', required=True, help='Visible .jsonl path relative to root; bound into plan and digest')
        if command in ('apply', 'rollback'):
            current.add_argument('--confirm', required=True)
    args = parser.parse_args()
    try:
        guard = RootGuard(args.root)
        if args.command == 'plan':
            result = make_plan(guard, read_mapping(args.mapping_file), args.log, args.max_entries)
        elif args.command == 'apply':
            result = apply(guard, read_json(Path(args.plan_file))[0], args.confirm, args.log)
        elif args.command == 'rollback-plan':
            result = rollback_plan(guard, args.log)
        else:
            result = rollback(guard, args.log, args.confirm)
        sys.stdout.write(encoded(result).decode('ascii'))
        return 0
    except (OSError, OperationError) as error:
        sys.stdout.write(encoded({'error': {'code': getattr(error, 'code', 'filesystem_error'), 'message': str(error)}}).decode('ascii'))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
