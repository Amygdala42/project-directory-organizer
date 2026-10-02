#!/usr/bin/env python3
"""Read-only verification of explicitly adopted project rules (Python 3.9+).

Policy is supplied as JSON, never inferred from a template or arbitrary prose.
Only explicitly selected Markdown link sources are read (at most 1 MiB each).
All other checks use inventory metadata. No files or caches are written.
"""
from __future__ import annotations

import sys
sys.dont_write_bytecode = True

import fnmatch
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import unicodedata
from urllib.parse import unquote, urlsplit

from folder_inventory import inventory, positive_integer, JsonArgumentParser
from path_safety import RootGuard, OperationError, fail, identity, is_reparse

POLICY_BYTES = 2 * 1024 * 1024
DOCUMENT_BYTES = 1024 * 1024
PROJECT_KEYS = {'id', 'path', 'purpose', 'status', 'language', 'environment', 'outputs', 'rules'}


def text_value(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        fail('invalid_policy', label + ' must be nonempty text of at most 2000 characters.')
    if any(unicodedata.category(c).startswith('C') for c in value):
        fail('invalid_policy', label + ' contains control characters.')
    return value


def relative_path(value):
    value = text_value(value, 'path').replace('\\', '/')
    parts = value.split('/')
    if any(p in {'', '.', '..'} or p != p.strip() or p.endswith('.') or len(p) > 255 for p in parts):
        fail('invalid_policy', 'Use an ordinary root-relative path without traversal.')
    if any(c in value for c in '<>:"|?*'):
        fail('invalid_policy', 'Path contains a forbidden character.')
    for part in parts:
        stem = part.split('.', 1)[0].upper()
        if stem in {'CON', 'PRN', 'AUX', 'NUL', 'CLOCK$', 'CONIN$', 'CONOUT$'} or re.fullmatch(r'(COM|LPT)[1-9¹²³]', stem):
            fail('invalid_policy', 'Path contains a reserved device name.')
    return value


def key(path):
    return path.casefold() if os.name == 'nt' else path


def inside(path, parent):
    return key(path) == key(parent) or key(path).startswith(key(parent) + '/')


def records(value, label):
    if not isinstance(value, list) or len(value) > 1000:
        fail('invalid_policy', label + ' must be a list of at most 1000 items.')
    return value


def fields(record, required, optional=()):
    if not isinstance(record, dict) or not required <= set(record) or set(record) - required - set(optional):
        fail('invalid_policy', 'Invalid fields in a policy record.')


def validate_policy(policy):
    fields(policy, {'schema_version'}, {'paths', 'names', 'links', 'projects'})
    if type(policy['schema_version']) is not int or policy['schema_version'] != 1:
        fail('invalid_policy', 'Unsupported policy schema.')
    normalized = {'schema_version': 1}
    for group in ('paths', 'names', 'links', 'projects'):
        normalized[group] = []
        seen = set()
        for original in records(policy.get(group, []), group):
            if not isinstance(original, dict):
                fail('invalid_policy', 'Policy records must be objects.')
            record = dict(original)
            if group == 'paths':
                fields(record, {'path', 'kind', 'state'})
                if record['kind'] not in ('file', 'directory') or record['state'] not in ('present', 'planned'):
                    fail('invalid_policy', 'Invalid path kind or state.')
                record['path'] = relative_path(record['path'])
                unique = key(record['path'])
            elif group == 'names':
                fields(record, {'path', 'kind', 'pattern'}, {'exceptions'})
                record['path'] = relative_path(record['path'])
                if record['kind'] not in ('file', 'directory', 'any'):
                    fail('invalid_policy', 'Invalid naming kind.')
                pattern = text_value(record['pattern'], 'pattern')
                if len(pattern) > 200 or '/' in pattern or '\\' in pattern:
                    fail('invalid_policy', 'Naming patterns match one basename, at most 200 characters.')
                record['exceptions'] = records(record.get('exceptions', []), 'exceptions')
                for exception in record['exceptions']:
                    if '/' in relative_path(exception):
                        fail('invalid_policy', 'Naming exceptions must be literal basenames.')
                unique = (key(record['path']), record['kind'], pattern)
            elif group == 'links':
                fields(record, {'source', 'target'})
                for name in ('source', 'target'):
                    record[name] = relative_path(record[name])
                unique = (key(record['source']), key(record['target']))
            else:
                fields(record, PROJECT_KEYS)
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', text_value(record['id'], 'id')):
                    fail('invalid_policy', 'Project ids use ASCII letters, digits, underscores and hyphens, starting with a letter or digit.')
                record['path'] = relative_path(record['path'])
                text_value(record['purpose'], 'purpose')
                if record['status'] not in ('planned', 'active', 'archived') or record['language'] not in ('chinese', 'english'):
                    fail('invalid_policy', 'Invalid project status or language.')
                if record['environment'] is not None:
                    record['environment'] = relative_path(record['environment'])
                record['outputs'] = [relative_path(p) for p in records(record['outputs'], 'outputs')]
                for rule in records(record['rules'], 'rules'):
                    text_value(rule, 'rule')
                unique = record['id'].casefold()
            if unique in seen:
                fail('invalid_policy', 'Duplicate policy record.')
            seen.add(unique)
            normalized[group].append(record)
    roots = [key(p['path']) for p in normalized['projects']]
    if len(set(roots)) != len(roots):
        fail('invalid_policy', 'Different projects cannot claim the same root.')
    return normalized


def read_document(guard, name, scanned):
    """Read a declared source through checked ancestors and a bounded fd."""
    guard.check()
    path = guard.path
    for part in name.split('/'):
        path = path / part
        info = path.lstat()
        if is_reparse(info):
            fail('reparse_point', 'A document path became a link; it was not read.')
        if path != guard.path / name and not stat.S_ISDIR(info.st_mode):
            fail('document_changed', 'A document ancestor is no longer a directory.')
    if not stat.S_ISREG(info.st_mode):
        fail('document_changed', 'The declared source is no longer a regular file.')
    if info.st_size > DOCUMENT_BYTES:
        fail('document_limit', 'Link source exceeds the 1 MiB read limit.')
    if info.st_size != scanned['size_bytes']:
        fail('document_changed', 'The link source changed after inventory.')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    try:
        opened = os.fstat(fd)
        if is_reparse(opened) or identity(opened) != identity(info) or not stat.S_ISREG(opened.st_mode):
            fail('document_changed', 'The link source changed while opening.')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(DOCUMENT_BYTES + 1)
        if len(data) > DOCUMENT_BYTES:
            fail('document_limit', 'Link source exceeds the 1 MiB read limit.')
        after = os.fstat(fd)
        current = path.lstat()
        if is_reparse(current) or identity(current) != identity(opened) or (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
            fail('document_changed', 'The link source changed while reading.')
        guard.check()
        return data.decode('utf-8-sig')
    finally:
        os.close(fd)


def inline_targets(document, source):
    """Support ordinary inline Markdown links, ignoring fenced/inline code."""
    lines = []
    fence = None
    for line in document.splitlines():
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not line[marker.end():].strip():
                fence = None
            continue
        if fence is None and not line.startswith(('    ', '\t')):
            lines.append(line)
    visible = re.sub(r'<!--.*?-->', '', '\n'.join(lines), flags=re.S)
    # Code spans may wrap lines, but cannot cross Markdown paragraph boundaries.
    visible = ''.join(
        re.sub(r'(?<!`)(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)', '', paragraph)
        for paragraph in re.split(r'(\n[ \t]*\n)', visible)
    )

    def escaped(index):
        start = index
        while start > 0 and visible[start - 1] == '\\':
            start -= 1
        return (index - start) % 2 == 1

    # Parse balanced destination parentheses rather than assuming paths lack them.
    for match in re.finditer(r'(?<!!)\[[^\]\n]+\]\(\s*', visible):
        if escaped(match.start()):
            continue
        start = end = match.end()
        if start >= len(visible):
            continue
        if visible[start] == '<':
            start += 1
            end = visible.find('>', start)
            if end < 0 or '\n' in visible[start:end]:
                continue
            tail = end + 1
        else:
            depth = 0
            while end < len(visible):
                char = visible[end]
                if not escaped(end):
                    if char == '(':
                        depth += 1
                    elif char == ')':
                        if depth == 0:
                            break
                        depth -= 1
                    elif char.isspace():
                        break
                end += 1
            if depth:
                continue
            tail = end
        if not re.match(r'\s*(?:["\'][^\n]*?["\']\s*)?\)', visible[tail:]):
            continue
        target = re.sub(r'\\([!"#$%&\'()*+,\-./:;<=>?@\[\\\]^_`{|}~])', r'\1', visible[start:end])
        url = urlsplit(target)
        if url.scheme or url.netloc or not url.path or url.path.startswith('/'):
            continue
        decoded = unquote(url.path).replace('\\', '/')
        normalized = posixpath.normpath(posixpath.join(posixpath.dirname(source), decoded))
        if normalized != '..' and not normalized.startswith('../') and not normalized.startswith('/'):
            yield normalized


def verify(root, policy, max_depth=None, max_entries=10000):
    policy = validate_policy(policy)
    guard = RootGuard(root)
    scan = inventory(root, max_depth=max_depth, max_entries=max_entries)
    entries = {key(item['path']): item for item in scan['entries']}
    checks = []

    def add(status, code, path, message):
        checks.append({'status': status, 'code': code, 'path': path, 'message': message})

    def unseen(path):
        return any(x['path'] == '.' or inside(path, x['path']) for x in [*scan['omissions'], *scan['errors']])

    def path_check(path, kind, planned=False):
        entry = entries.get(key(path))
        if entry is None:
            if unseen(path):
                add('unverified', 'not_scanned', path, 'The path is in an omitted or failed scan range.')
            elif planned:
                add('pass', 'planned_absent', path, 'The path is explicitly planned and has not been created.')
            else:
                add('deviation', 'path_missing', path, 'An explicitly required path is absent.')
            return None
        if entry['type'] in ('reparse_point', 'unavailable', 'other'):
            add('unverified', 'not_scanned', path, 'The path was not inspected as a regular file or directory.')
        elif entry['type'] != kind:
            add('deviation', 'wrong_type', path, 'Expected ' + kind + ', found ' + entry['type'] + '.')
        else:
            add('pass', 'path_present', path, 'The declared path and type exist; this does not verify their business purpose.')
        return entry

    for rule in policy['paths']:
        path_check(rule['path'], rule['kind'], rule['state'] == 'planned')
    for project in policy['projects']:
        path = project['path']
        planned = project['status'] == 'planned'
        path_check(path, 'directory', planned)
        for name in ([project['environment']] if project['environment'] else []) + project['outputs']:
            owners = [p for p in policy['projects'] if inside(name, p['path'])]
            owner = max(owners, key=lambda p: len(p['path']), default=None)
            if owner is None or owner['id'] != project['id']:
                add('deviation', 'ownership_mismatch', name, 'This location belongs to a different or more specific project; shared ownership requires separate human review.')
            else:
                path_check(name, 'directory', planned)
        if project['environment']:
            add('unverified', 'environment_installation_unverified', project['environment'], 'Folder presence does not prove installation or a working environment.')
        add('unverified', 'project_semantics_unverified', path, 'Purpose, archive status, language exceptions and free-text rules require human review.')
    for rule in policy['names']:
        parent = entries.get(key(rule['path']))
        if not parent or parent['type'] != 'directory':
            path_check(rule['path'], 'directory')
            continue
        if parent.get('children_status') != 'enumerated':
            add('unverified', 'not_scanned', rule['path'], 'Naming results cover only enumerated direct children.')
        candidates = [e for e in entries.values() if key(str(PurePosixPath(e['path']).parent)) == key(rule['path'])]
        for entry in sorted(candidates, key=lambda e: e['path']):
            if entry['type'] not in ('file', 'directory') or rule['kind'] not in ('any', entry['type']):
                continue
            basename = PurePosixPath(entry['path']).name
            accepted = basename in rule['exceptions'] or fnmatch.fnmatchcase(basename, rule['pattern'])
            add('pass' if accepted else 'deviation', 'name_allowed' if accepted else 'name_mismatch', entry['path'], 'Compared with the explicitly supplied basename pattern and exceptions.')
        if not candidates and parent.get('children_status') == 'enumerated':
            add('pass', 'no_names_to_check', rule['path'], 'The directory has no direct children to check.')
    for rule in policy['links']:
        source, target = rule['source'], rule['target']
        item = entries.get(key(source))
        if not item or item['type'] != 'file':
            path_check(source, 'file')
            continue
        try:
            document = read_document(guard, source, item)
            found = any(key(p) == key(target) for p in inline_targets(document, source))
            if not found:
                add('deviation', 'link_missing', source, 'No supported inline Markdown link to ' + target + ' was found outside code/comments.')
            else:
                destination = entries.get(key(target))
                if destination and destination['type'] == 'file':
                    add('pass', 'link_present', source, 'The inline link points to an existing local file: ' + target)
                else:
                    path_check(target, 'file')
        except (OperationError, OSError, UnicodeError, ValueError) as error:
            add('unverified', getattr(error, 'code', 'document_unreadable'), source, str(error))
    if not checks:
        add('unverified', 'no_rules', '.', 'No applicable explicit rules were supplied.')
    guard.check()
    counts = {status: sum(c['status'] == status for c in checks) for status in ('pass', 'deviation', 'unverified')}
    return {'schema_version': 1, 'status': 'verified', 'root': scan['root'],
            'complete': scan['complete'], 'compliant': scan['complete'] and not counts['deviation'] and not counts['unverified'],
            'summary': counts, 'checks': checks, 'omissions': scan['omissions'], 'errors': scan['errors'],
            'limits': scan['limits'], 'policy': {'writes_files': False, 'follow_reparse_points': False,
                                               'read_file_contents': 'only declared link sources', 'document_byte_limit': DOCUMENT_BYTES}}


def unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            fail('invalid_policy', 'Duplicate JSON keys are not accepted.')
        result[name] = value
    return result


def read_policy(value):
    if value == '-':
        raw = sys.stdin.buffer.read(POLICY_BYTES + 1)
    else:
        with open(value, 'rb') as stream:
            raw = stream.read(POLICY_BYTES + 1)
    if len(raw) > POLICY_BYTES:
        fail('invalid_policy', 'Policy input exceeds 2 MiB.')
    return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique_object)


def main(argv=None):
    parser = JsonArgumentParser(description=__doc__)
    parser.add_argument('root')
    parser.add_argument('--policy-file', required=True, help='Explicit adopted policy JSON file or - for stdin')
    parser.add_argument('--max-depth', type=positive_integer, default=None)
    parser.add_argument('--max-entries', type=positive_integer, default=10000)
    parser.add_argument('--strict', action='store_true', help='Exit 1 for deviations, unverified checks or incomplete scans')
    try:
        args = parser.parse_args(argv)
        result = verify(args.root, read_policy(args.policy_file), args.max_depth, args.max_entries)
        code = 1 if args.strict and not result['compliant'] else 0
    except (OperationError, OSError, ValueError, UnicodeError, RecursionError) as error:
        result = {'status': 'error', 'code': getattr(error, 'code', 'invalid_input'), 'message': str(error)}
        code = 2
    print(json.dumps(result, ensure_ascii=True))
    return code


if __name__ == '__main__':
    sys.exit(main())
