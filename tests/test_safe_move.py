"""Controlled-move regression tests; all writes stay in dedicated fixtures."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
TEST_HOME = Path(__file__).resolve().parent
SCRIPT = TEST_HOME.parent / 'skills/project-directory-organizer/scripts/safe_move.py'


def load_move_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location('safe_move_capacity_fixture', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


class SafeMoveTests(unittest.TestCase):
    def setUp(self):
        runs = Path(tempfile.gettempdir()) / 'project-directory-organizer-tests'
        runs.mkdir(exist_ok=True)
        self.case = Path(tempfile.mkdtemp(prefix='move-', dir=runs))
        self.root = self.case / 'root'
        self.root.mkdir()
        (self.root / 'reports').mkdir()
        (self.root / 'records').mkdir()
        self.write('draft.txt', 'draft')

    def write(self, name, contents):
        path = self.root / name
        self.assertTrue(path.absolute().is_relative_to(self.root.absolute()))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding='utf-8')
        return path

    def call(self, command, *args, success=True):
        result = subprocess.run([sys.executable, '-B', str(SCRIPT), command, str(self.root), *map(str, args)], capture_output=True, encoding='utf-8', errors='replace')
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        try:
            return json.loads(result.stdout)
        except ValueError:
            self.fail('Expected JSON response: ' + result.stdout + result.stderr)

    def item(self, source='draft.txt', destination='reports/draft.txt', risk='clear', group='report'):
        return {'source': source, 'destination': destination, 'risk': risk, 'group': group,
                'evidence': {'scope': 'Self-contained text artifact; project checked.',
                             'references': 'No incoming or outgoing references found; reviewed project usage.',
                             'completeness': 'All related files reviewed; no external or uncertain consumer.',
                             'checked_paths': ['draft.txt']}}

    def plan(self, *items, success=True, log='records/move.jsonl'):
        path = self.case / 'mapping.json'
        path.write_text(json.dumps({'items': list(items or [self.item()])}), encoding='utf-8')
        return self.call('plan', '--mapping-file', path, '--log', log, success=success)

    def apply(self, plan, success=True, confirm=None, log='records/move.jsonl'):
        path = self.case / 'plan.json'
        path.write_text(json.dumps(plan), encoding='utf-8')
        return self.call('apply', '--plan-file', path, '--confirm', confirm or plan['plan_digest'], '--log', log, success=success)

    def rollback(self, success=True):
        preview = self.call('rollback-plan', '--log', 'records/move.jsonl', success=success)
        if not success:
            return preview
        return self.call('rollback', '--log', 'records/move.jsonl', '--confirm', preview['rollback_digest'])

    def test_plan_is_read_only(self):
        before = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob('*'))
        result = self.plan()
        self.assertEqual(result['items'][0]['decision'], 'move')
        self.assertEqual(len(result['plan_digest']), 64)
        self.assertEqual(before, sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob('*')))

    @unittest.skipUnless(os.name == 'nt', 'Mutations require Windows no-overwrite rename.')
    def test_apply_and_rollback_preserve_file_bytes(self):
        plan = self.plan()
        self.assertEqual(self.apply(plan)['status'], 'applied')
        self.assertFalse((self.root / 'draft.txt').exists())
        self.assertEqual((self.root / 'reports/draft.txt').read_text(), 'draft')
        self.assertEqual(self.rollback()['status'], 'rolled_back')
        self.assertEqual((self.root / 'draft.txt').read_text(), 'draft')
        self.assertFalse((self.root / 'reports/draft.txt').exists())

    def test_risk_and_unknown_are_not_executable(self):
        self.write('other.txt', 'unknown')
        result = self.plan(self.item(risk='risk'), self.item('other.txt', 'reports/other.txt', risk='unknown', group='other'))
        self.assertTrue(all(i['decision'] == 'hold' for i in result['items']))
        self.assertEqual(self.apply(result, success=False)['error']['code'], 'nothing_to_move')
        self.assertFalse((self.root / 'records/move.jsonl').exists())

    def test_risky_related_item_blocks_whole_group(self):
        self.write('other.txt', 'related')
        result = self.plan(self.item(), self.item('other.txt', 'reports/other.txt', risk='unknown'))
        self.assertTrue(all(i['decision'] == 'hold' for i in result['items']))

    def test_rejects_empty_evidence_for_clear(self):
        item = self.item()
        item['evidence']['completeness'] = ''
        self.assertEqual(self.plan(item, success=False)['error']['code'], 'incomplete_assessment')

    def test_protected_original_code_environment_and_rules(self):
        for source in ['data/raw/input.txt', '原始数据/样本.csv', 'reference/paper.pdf', 'code/run.py', 'env/tool.exe', 'AGENTS.md', 'PROJECT_RULES.md']:
            with self.subTest(source=source):
                self.write(source, 'protected')
                item = self.item(source, 'reports/' + Path(source).name)
                self.assertEqual(self.plan(item, success=False)['error']['code'], 'protected_path')

    def test_hidden_protected_descendant_in_directory_package(self):
        self.write('package/.git/config', 'repository')
        self.assertEqual(self.plan(self.item('package', 'reports/package'), success=False)['error']['code'], 'protected_path')

    def test_rejects_overlap_and_missing_parent(self):
        self.write('package/file.txt', 'package')
        self.assertEqual(self.plan(self.item('package', 'reports/package'), self.item('package/file.txt', 'reports/file.txt', group='second'), success=False)['error']['code'], 'overlap')
        self.assertEqual(self.plan(self.item(destination='missing/draft.txt'), success=False)['error']['code'], 'missing_path')

    def test_rejects_collision_even_for_same_bytes(self):
        self.write('reports/draft.txt', 'draft')
        self.assertEqual(self.plan(success=False)['error']['code'], 'collision')

    def test_modified_plan_is_rejected(self):
        plan = self.plan()
        plan['items'][0]['destination'] = 'reports/changed.txt'
        self.assertEqual(self.apply(plan, success=False)['error']['code'], 'plan_digest')

    def test_wrong_confirmation_is_rejected(self):
        self.assertEqual(self.apply(self.plan(), success=False, confirm='0' * 64)['error']['code'], 'confirmation_mismatch')

    def test_same_approved_digest_cannot_write_another_log(self):
        plan = self.plan()
        result = self.apply(plan, success=False, log='records/unapproved.jsonl')
        self.assertEqual(result['error']['code'], 'log_mismatch')
        self.assertFalse((self.root / 'records/unapproved.jsonl').exists())
        self.assertFalse((self.root / 'records/move.jsonl').exists())
        self.assertTrue((self.root / 'draft.txt').exists())

    def test_reviewed_input_cannot_be_removed_from_redigested_plan(self):
        import hashlib
        plan = self.plan()
        plan['assessment_inputs'] = []
        content = {key: value for key, value in plan.items() if key != 'plan_digest'}
        plan['plan_digest'] = hashlib.sha256((json.dumps(content, ensure_ascii=True, sort_keys=True, separators=(',', ':')) + '\n').encode()).hexdigest()
        self.assertEqual(self.apply(plan, success=False)['error']['code'], 'invalid_plan')

    def test_path_traversal_and_absolute_paths_are_refused(self):
        for source in ['../draft.txt', str(self.root / 'draft.txt'), 'reports/../draft.txt']:
            with self.subTest(source=source):
                self.assertEqual(self.plan(self.item(source), success=False)['error']['code'], 'unsafe_path')

    def test_wrong_root_is_refused(self):
        plan = self.plan()
        previous = self.root
        self.root = self.case / 'other-root'
        self.root.mkdir()
        try:
            self.assertEqual(self.apply(plan, success=False)['error']['code'], 'wrong_root')
        finally:
            self.root = previous

    def test_mapping_from_stdin_is_read_only(self):
        result = subprocess.run([sys.executable, '-B', str(SCRIPT), 'plan', str(self.root), '--mapping-file', '-', '--log', 'records/move.jsonl'],
                                input=json.dumps({'items': [self.item()]}), capture_output=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['items'][0]['decision'], 'move')
        self.assertFalse(list((self.root / 'records').iterdir()))

    def test_changed_source_is_rejected_before_log_write(self):
        plan = self.plan()
        self.write('draft.txt', 'changed')
        self.assertEqual(self.apply(plan, success=False)['error']['code'], 'changed')
        self.assertFalse((self.root / 'records/move.jsonl').exists())

    def test_changed_assessment_input_is_rejected(self):
        self.write('usage.txt', 'reviewed reference evidence')
        item = self.item()
        item['evidence']['checked_paths'].append('usage.txt')
        plan = self.plan(item)
        self.write('usage.txt', 'new dependency')
        self.assertEqual(self.apply(plan, success=False)['error']['code'], 'assessment_changed')

    @unittest.skipUnless(os.name == 'nt', 'Mutations require Windows.')
    def test_complete_directory_package_is_moved_and_rolled_back(self):
        self.write('bundle/a.txt', 'a')
        self.write('bundle/nested/b.txt', 'b')
        plan = self.plan(self.item('bundle', 'reports/bundle'))
        self.apply(plan)
        self.assertEqual((self.root / 'reports/bundle/nested/b.txt').read_text(), 'b')
        self.rollback()
        self.assertEqual((self.root / 'bundle/nested/b.txt').read_text(), 'b')

    @unittest.skipUnless(os.name == 'nt', 'Mutations require Windows.')
    def test_rollback_refuses_changed_or_conflicting_content(self):
        self.apply(self.plan())
        self.write('draft.txt', 'new occupant')
        self.assertEqual(self.rollback(success=False)['error']['code'], 'collision')

    @unittest.skipUnless(os.name == 'nt', 'Mutations require Windows.')
    def test_apply_keeps_risky_independent_group_in_place(self):
        self.write('other.txt', 'held')
        result = self.apply(self.plan(self.item(), self.item('other.txt', 'reports/other.txt', risk='risk', group='separate')))
        self.assertEqual(result['moved_count'], 1)
        self.assertEqual(result['held_count'], 1)
        self.assertTrue((self.root / 'other.txt').exists())
        self.assertFalse((self.root / 'reports/other.txt').exists())

    @unittest.skipUnless(os.name == 'nt', 'Mutations require Windows.')
    def test_rollback_requires_exact_preview_digest(self):
        self.apply(self.plan())
        self.assertEqual(self.call('rollback', '--log', 'records/move.jsonl', '--confirm', '0' * 64, success=False)['error']['code'], 'confirmation_mismatch')
        self.assertTrue((self.root / 'reports/draft.txt').exists())

    @unittest.skipUnless(os.name == 'nt', 'Mutations require Windows.')
    def test_rollback_refuses_modified_moved_file(self):
        self.apply(self.plan())
        self.write('reports/draft.txt', 'changed after move')
        self.assertEqual(self.rollback(success=False)['error']['code'], 'changed')

    @unittest.skipUnless(os.name == 'nt', 'Mutations require Windows.')
    def test_rollback_refuses_truncated_journal(self):
        self.apply(self.plan())
        path = self.root / 'records/move.jsonl'
        path.write_bytes(b'\n'.join(path.read_bytes().splitlines()[:-1]) + b'\n')
        self.assertEqual(self.rollback(success=False)['error']['code'], 'incomplete_log')

    @unittest.skipUnless(os.name == 'nt', 'Failure recovery uses Windows moves.')
    def test_partial_failure_stops_and_retains_auditable_log(self):
        import importlib.util
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location('safe_move_fixture_module', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(SCRIPT.parent))
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path.pop(0)
        self.write('second.txt', 'second')
        plan = self.plan(self.item(), self.item('second.txt', 'reports/second.txt', group='second'))
        actual_move = module.move_exact
        calls = [0]
        def interrupted_move(guard, source, destination):
            calls[0] += 1
            if calls[0] == 2:
                raise PermissionError('Fixture simulates a destination becoming unavailable')
            return actual_move(guard, source, destination)
        with patch.object(module, 'move_exact', interrupted_move):
            with self.assertRaises(PermissionError):
                module.apply(module.RootGuard(self.root), plan, plan['plan_digest'], 'records/move.jsonl')
        self.assertTrue((self.root / 'reports/draft.txt').exists())
        self.assertTrue((self.root / 'second.txt').exists())
        self.assertFalse((self.root / 'reports/second.txt').exists())
        events = [json.loads(line) for line in (self.root / 'records/move.jsonl').read_text().splitlines()]
        self.assertEqual(events[-1]['event'], 'apply_failed')
        self.assertEqual(self.rollback(success=False)['error']['code'], 'incomplete_log')

    def test_destination_appearing_after_plan_is_not_overwritten(self):
        plan = self.plan()
        self.write('reports/draft.txt', 'new arrival')
        self.assertEqual(self.apply(plan, success=False)['error']['code'], 'collision')
        self.assertEqual((self.root / 'reports/draft.txt').read_text(), 'new arrival')

    def test_held_nested_object_cannot_be_moved_by_other_group(self):
        self.write('bundle/a.txt', 'nested held artifact')
        self.assertEqual(self.plan(self.item('bundle', 'reports/bundle'),
                                   self.item('bundle/a.txt', 'reports/a.txt', risk='unknown', group='different'), success=False)['error']['code'], 'overlap')

    @unittest.skipUnless(os.name == 'nt', 'Windows junction regression.')
    def test_junction_source_is_refused(self):
        (self.root / 'real').mkdir()
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(self.root / 'linked'), str(self.root / 'real')], capture_output=True)
        self.assertEqual(result.returncode, 0, repr(result.stderr))
        self.assertEqual(self.plan(self.item('linked', 'reports/linked'), success=False)['error']['code'], 'reparse_point')

    @unittest.skipUnless(os.name == 'nt', 'Windows held-junction regression.')
    def test_held_junction_does_not_block_independent_clear_group(self):
        (self.root / 'real').mkdir()
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(self.root / 'linked'), str(self.root / 'real')], capture_output=True)
        self.assertEqual(result.returncode, 0, repr(result.stderr))
        held = self.item('linked', 'reports/linked', risk='unknown', group='held-link')
        plan = self.plan(self.item(), held)
        self.assertEqual([item['decision'] for item in plan['items']], ['move', 'hold'])
        self.assertEqual(plan['items'][1]['entries'], [])
        result = self.apply(plan)
        self.assertEqual(result['moved_count'], 1)
        self.assertTrue((self.root / 'linked').exists())
        self.assertTrue((self.root / 'reports/draft.txt').exists())

    def test_missing_held_object_does_not_block_independent_clear_group(self):
        held = self.item('missing/held.txt', 'reports/held.txt', risk='unknown', group='missing')
        plan = self.plan(self.item(), held)
        self.assertEqual([item['decision'] for item in plan['items']], ['move', 'hold'])
        self.assertEqual(plan['items'][1]['entries'], [])

    def test_missing_held_descendant_still_blocks_overlap(self):
        self.write('bundle/a.txt', 'complete artifact')
        self.assertEqual(self.plan(self.item('bundle', 'reports/bundle'),
                                   self.item('bundle/missing/held.txt', 'reports/held.txt', risk='unknown', group='separate'), success=False)['error']['code'], 'overlap')

    def test_log_cannot_be_inside_moved_package(self):
        self.write('bundle/a.txt', 'a')
        self.assertEqual(self.plan(self.item('bundle', 'reports/bundle'), success=False, log='bundle/move.jsonl')['error']['code'], 'overlap')

    def test_log_cannot_be_inside_assessment_evidence(self):
        item = self.item()
        item['evidence']['checked_paths'].append('records')
        self.assertEqual(self.plan(item, success=False)['error']['code'], 'log_evidence_overlap')
        self.assertFalse((self.root / 'records/move.jsonl').exists())

    def test_log_rejects_equal_and_nested_held_sources_and_destinations(self):
        for key in ('source', 'destination'):
            for held_path in ('records/move.jsonl', 'records', 'records/move.jsonl/missing.txt'):
                with self.subTest(key=key, held_path=held_path):
                    held = self.item('missing.txt', 'reports/held.txt', risk='unknown', group='held')
                    held[key] = held_path
                    result = self.plan(self.item(), held, success=False)
                    self.assertEqual(result['error']['code'], 'overlap')
                    self.assertTrue((self.root / 'draft.txt').exists())
                    self.assertFalse((self.root / 'records/move.jsonl').exists())
                    self.assertFalse((self.root / 'reports/draft.txt').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows move regression.')
    def test_apply_rechecks_held_log_overlap_before_any_write(self):
        module = load_move_module()
        held = self.item('missing.txt', 'reports/held.txt', risk='unknown', group='held')
        plan = self.plan(self.item(), held)
        # Reproduce an otherwise valid plan produced by the previous version.
        plan['items'][1]['source'] = 'records/move.jsonl'
        plan['plan_digest'] = module.content_digest(plan)
        result = self.apply(plan, success=False)
        self.assertEqual(result['error']['code'], 'overlap')
        self.assertTrue((self.root / 'draft.txt').exists())
        self.assertFalse((self.root / 'records/move.jsonl').exists())
        self.assertFalse((self.root / 'reports/draft.txt').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows legacy-journal recovery regression.')
    def test_existing_complete_log_inside_held_path_can_still_rollback(self):
        module = load_move_module()
        held = self.item('missing.txt', 'reports/held.txt', risk='unknown', group='held')
        plan = self.plan(self.item(), held)
        plan['items'][1]['source'] = 'records/move.jsonl'
        plan['plan_digest'] = module.content_digest(plan)
        # Construct the real completed filesystem/journal state that the old
        # version allowed, without asking the current apply to allow it again.
        os.rename(self.root / 'draft.txt', self.root / 'reports/draft.txt')
        events = [{'event': 'start', 'tool': module.TOOL, 'plan': plan},
                  {'event': 'apply_moving', 'index': 0},
                  {'event': 'apply_moved', 'index': 0}, {'event': 'applied'}]
        log = self.root / 'records/move.jsonl'
        log.write_bytes(b''.join(module.encoded(event) for event in events))
        result = self.rollback()
        self.assertEqual(result['status'], 'rolled_back')
        self.assertEqual(result['held_count'], 1)
        self.assertEqual((self.root / 'draft.txt').read_text(), 'draft')
        self.assertFalse((self.root / 'reports/draft.txt').exists())
        self.assertEqual(json.loads(log.read_text().splitlines()[-1]), {'event': 'rolled_back'})

    @unittest.skipUnless(os.name == 'nt', 'Windows move regression.')
    def test_overlapping_held_items_still_allow_an_independent_move(self):
        self.write('retained/a.txt', 'held contents')
        plan = self.plan(self.item(),
                         self.item('retained', 'reports/retained', risk='risk', group='held-one'),
                         self.item('retained/a.txt', 'reports/retained/a.txt', risk='unknown', group='held-two'))
        self.assertEqual([item['decision'] for item in plan['items']], ['move', 'hold', 'hold'])
        self.assertEqual(self.apply(plan)['held_count'], 2)
        self.assertEqual((self.root / 'retained/a.txt').read_text(), 'held contents')
        self.assertEqual((self.root / 'reports/draft.txt').read_text(), 'draft')
        self.assertEqual(self.rollback()['status'], 'rolled_back')

    def measured_roundtrip_journal_size(self, module):
        """Measure real journal writes, independent of the capacity estimator."""
        plan = self.plan(log='records/probe.jsonl')
        guard = module.RootGuard(self.root)
        module.apply(guard, plan, plan['plan_digest'], 'records/probe.jsonl')
        preview = module.rollback_plan(guard, 'records/probe.jsonl')
        module.rollback(guard, 'records/probe.jsonl', preview['rollback_digest'])
        return (self.root / 'records/probe.jsonl').stat().st_size

    @unittest.skipUnless(os.name == 'nt', 'Windows journal capacity regression.')
    def test_apply_reserves_rollback_log_space_before_any_write(self):
        from unittest.mock import patch
        module = load_move_module()
        required = self.measured_roundtrip_journal_size(module)
        # Equal-length log names preserve the serialized size after the probe.
        plan = self.plan(log='records/move0.jsonl')
        with patch.object(module, 'MAX_JOURNAL_BYTES', required - 1, create=True):
            with self.assertRaises(module.OperationError) as raised:
                module.apply(module.RootGuard(self.root), plan, plan['plan_digest'], 'records/move0.jsonl')
        self.assertEqual(raised.exception.code, 'journal_limit')
        self.assertEqual((self.root / 'draft.txt').read_text(), 'draft')
        self.assertFalse((self.root / 'reports/draft.txt').exists())
        self.assertFalse((self.root / 'records/move0.jsonl').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows journal capacity regression.')
    def test_journal_size_boundary_can_apply_and_rollback(self):
        from unittest.mock import patch
        module = load_move_module()
        required = self.measured_roundtrip_journal_size(module)
        plan = self.plan(log='records/move0.jsonl')
        guard = module.RootGuard(self.root)
        with patch.object(module, 'MAX_JOURNAL_BYTES', required, create=True):
            self.assertEqual(module.apply(guard, plan, plan['plan_digest'], 'records/move0.jsonl')['status'], 'applied')
            self.assertEqual((self.root / 'reports/draft.txt').read_text(), 'draft')
            preview = module.rollback_plan(guard, 'records/move0.jsonl')
            self.assertEqual(module.rollback(guard, 'records/move0.jsonl', preview['rollback_digest'])['status'], 'rolled_back')
        self.assertEqual((self.root / 'records/move0.jsonl').stat().st_size, required)
        self.assertEqual((self.root / 'draft.txt').read_text(), 'draft')
        self.assertFalse((self.root / 'reports/draft.txt').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows journal capacity regression.')
    def test_existing_complete_journal_at_read_limit_can_still_rollback(self):
        from unittest.mock import patch
        module = load_move_module()
        plan = self.plan()
        guard = module.RootGuard(self.root)
        module.apply(guard, plan, plan['plan_digest'], 'records/move.jsonl')
        applied_size = (self.root / 'records/move.jsonl').stat().st_size
        with patch.object(module, 'MAX_JOURNAL_BYTES', applied_size, create=True):
            preview = module.rollback_plan(guard, 'records/move.jsonl')
            self.assertEqual(module.rollback(guard, 'records/move.jsonl', preview['rollback_digest'])['status'], 'rolled_back')
        self.assertEqual((self.root / 'draft.txt').read_text(), 'draft')

    @unittest.skipUnless(os.name == 'nt', 'Windows journal capacity regression.')
    def test_reader_enforces_the_same_journal_size_limit(self):
        from unittest.mock import patch
        module = load_move_module()
        plan = self.plan()
        guard = module.RootGuard(self.root)
        module.apply(guard, plan, plan['plan_digest'], 'records/move.jsonl')
        raw = (self.root / 'records/move.jsonl').read_bytes()
        with patch.object(module, 'MAX_JOURNAL_BYTES', len(raw) - 1, create=True):
            with self.assertRaises(module.OperationError) as raised:
                module.rollback_plan(guard, 'records/move.jsonl')
        self.assertEqual(raised.exception.code, 'invalid_log')
        self.assertEqual((self.root / 'records/move.jsonl').read_bytes(), raw)
        self.assertFalse((self.root / 'draft.txt').exists())
        self.assertEqual((self.root / 'reports/draft.txt').read_text(), 'draft')

    def test_replaced_log_parent_is_rejected(self):
        plan = self.plan()
        (self.root / 'records').rename(self.root / 'previous-records')
        (self.root / 'records').mkdir()
        self.assertEqual(self.apply(plan, success=False)['error']['code'], 'log_parent_changed')
        self.assertFalse((self.root / 'records/move.jsonl').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows short-path regression.')
    def test_short_path_cannot_bypass_protected_reference(self):
        import ctypes
        path = self.write('reference materials/input.txt', 'original')
        # Exact protected directory uses a long fixed Chinese role name.
        path = self.write('参考资料/原始材料.txt', 'original')
        buffer = ctypes.create_unicode_buffer(32768)
        size = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer))
        self.assertTrue(size, 'Could not obtain fixture path name')
        alias = '/'.join(Path(buffer.value).parts[-2:])
        self.assertEqual(self.plan(self.item(alias, 'reports/original.txt'), success=False)['error']['code'], 'protected_path')


if __name__ == '__main__':
    unittest.main()
