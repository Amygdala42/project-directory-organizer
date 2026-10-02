"""Behavior tests for explicit-policy, read-only rule verification."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
SOURCE = Path(__file__).resolve().parents[1] / 'skills/project-directory-organizer/scripts'


class VerifyTests(unittest.TestCase):
    def setUp(self):
        runs = Path(tempfile.gettempdir()) / 'project-directory-organizer-tests'
        runs.mkdir(exist_ok=True)
        self.case = Path(tempfile.mkdtemp(prefix='verify-', dir=runs))
        self.root = self.case / 'target'
        self.root.mkdir()
        self.bundle = self.case / 'scripts'
        self.bundle.mkdir()
        for source in SOURCE.glob('*.py'):
            shutil.copyfile(source, self.bundle / source.name)
        self.script = self.bundle / 'directory_verify.py'

    def file(self, path, content='sample'):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
        return target

    def call(self, policy, *args, code=0):
        self.assertTrue(self.script.exists(), 'Read-only policy verifier is missing')
        result = subprocess.run([sys.executable, str(self.script), str(self.root),
                                 '--policy-file', '-', *args],
                                input=json.dumps(policy), encoding='utf-8', capture_output=True)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return json.loads(result.stdout)

    @staticmethod
    def policy(**parts):
        return {'schema_version': 1, **parts}

    def test_adopted_paths_and_planned_environment_are_read_only(self):
        self.file('code/analyze.py', 'print(1)')
        self.file('code/uv.lock')
        policy = self.policy(paths=[{'path': 'code', 'kind': 'directory', 'state': 'present'},
                                    {'path': 'env', 'kind': 'directory', 'state': 'planned'}])
        before = {str(p.relative_to(self.case)): p.read_bytes() for p in self.case.rglob('*') if p.is_file()}
        report = self.call(policy)
        after = {str(p.relative_to(self.case)): p.read_bytes() for p in self.case.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(report['summary']['deviation'], 0)
        self.assertEqual({c['code'] for c in report['checks']}, {'path_present', 'planned_absent'})

    def test_missing_required_path_and_wrong_type_fail_strict(self):
        self.file('reports')
        policy = self.policy(paths=[{'path': 'reports', 'kind': 'directory', 'state': 'present'},
                                    {'path': 'code', 'kind': 'directory', 'state': 'present'}])
        report = self.call(policy, '--strict', code=1)
        self.assertEqual(report['summary']['deviation'], 2)
        self.assertFalse(report['compliant'])

    def test_omitted_path_is_unknown_not_missing(self):
        self.file('.venv/config.json')
        policy = self.policy(paths=[{'path': '.venv/config.json', 'kind': 'file', 'state': 'present'}])
        report = self.call(policy, '--strict', code=1)
        self.assertFalse(report['complete'])
        self.assertEqual(report['summary']['deviation'], 0)
        self.assertEqual(report['checks'][0]['code'], 'not_scanned')

    def test_max_depth_and_entry_limits_preserve_unknown_scope(self):
        self.file('one/two/a.txt')
        policy = self.policy(paths=[{'path': 'one/two/a.txt', 'kind': 'file', 'state': 'present'}])
        for args in [('--max-depth', '1'), ('--max-entries', '1')]:
            with self.subTest(args=args):
                report = self.call(policy, *args)
                self.assertEqual(report['checks'][0]['status'], 'unverified')
                self.assertTrue(report['omissions'])

    def test_naming_checks_only_selected_direct_children_and_exceptions(self):
        self.file('reports/bad name.txt')
        self.file('reports/README.md')
        self.file('reports/native/bad nested.txt')
        self.file('other/another bad name.txt')
        policy = self.policy(names=[{'path': 'reports', 'kind': 'file', 'pattern': '*-*.txt',
                                    'exceptions': ['README.md']}])
        report = self.call(policy)
        bad = [c['path'] for c in report['checks'] if c['status'] == 'deviation']
        self.assertEqual(bad, ['reports/bad name.txt'])

    def test_local_rule_link_is_checked_without_counting_fenced_example(self):
        self.file('PROJECT_RULES.md', '# Rules')
        source = self.file('AGENTS.md', '```md\nRead [rules](PROJECT_RULES.md)\n```\n')
        policy = self.policy(links=[{'source': 'AGENTS.md', 'target': 'PROJECT_RULES.md'}])
        first = self.call(policy)
        self.assertEqual(first['checks'][0]['code'], 'link_missing')
        source.write_text('Read [rules](<PROJECT_RULES.md>).\n', encoding='utf-8')
        second = self.call(policy)
        self.assertEqual(second['checks'][0]['code'], 'link_present')

    def test_nested_percent_encoded_relative_link(self):
        self.file('规则.md')
        self.file('docs/AGENTS.md', '[规则](../%E8%A7%84%E5%88%99.md)')
        report = self.call(self.policy(links=[{'source': 'docs/AGENTS.md', 'target': '规则.md'}]))
        self.assertEqual(report['checks'][0]['status'], 'pass')

    def test_escaped_and_multiline_code_links_do_not_satisfy_policy(self):
        self.file('PROJECT_RULES.md')
        policy = self.policy(links=[{'source': 'AGENTS.md', 'target': 'PROJECT_RULES.md'}])
        for text in ['\\[rules](PROJECT_RULES.md)', '`example\n[rules](PROJECT_RULES.md)\n`']:
            with self.subTest(text=text):
                self.file('AGENTS.md', text)
                result = self.call(policy, '--strict', code=1)
                self.assertEqual(result['checks'][0]['code'], 'link_missing')

    def test_balanced_parentheses_in_link_filename(self):
        self.file('PROJECT_(RULES).md')
        self.file('AGENTS.md', '[rules](PROJECT_(RULES).md)')
        report = self.call(self.policy(links=[{'source': 'AGENTS.md', 'target': 'PROJECT_(RULES).md'}]))
        self.assertTrue(report['compliant'])

    def test_unmatched_backticks_in_other_paragraphs_do_not_hide_link(self):
        self.file('PROJECT_RULES.md')
        self.file('AGENTS.md', '`unfinished\n\n[rules](PROJECT_RULES.md)\n\n`another paragraph')
        report = self.call(self.policy(links=[{'source': 'AGENTS.md', 'target': 'PROJECT_RULES.md'}]))
        self.assertTrue(report['compliant'])

    def test_parent_project_cannot_claim_nested_project_environment(self):
        (self.root / 'alpha/beta/env').mkdir(parents=True)
        base = {'purpose': 'code', 'status': 'active', 'language': 'english',
                'environment': 'alpha/beta/env', 'outputs': [], 'rules': []}
        projects = [dict(base, id='alpha', path='alpha'), dict(base, id='beta', path='alpha/beta')]
        report = self.call(self.policy(projects=projects))
        conflicts = [c for c in report['checks'] if c['code'] == 'ownership_mismatch']
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]['path'], 'alpha/beta/env')

    def test_large_link_source_is_unverified(self):
        self.file('PROJECT_RULES.md')
        self.file('AGENTS.md', 'x' * (1024 * 1024 + 1))
        report = self.call(self.policy(links=[{'source': 'AGENTS.md', 'target': 'PROJECT_RULES.md'}]))
        self.assertEqual(report['checks'][0]['code'], 'document_limit')
        self.assertEqual(report['checks'][0]['status'], 'unverified')

    def test_project_status_and_ownership_never_claim_environment_installed(self):
        (self.root / 'alpha/env').mkdir(parents=True)
        project = {'id': 'alpha', 'path': 'alpha', 'purpose': 'code', 'status': 'active',
                   'language': 'english', 'environment': 'alpha/env', 'outputs': ['beta/reports'], 'rules': []}
        report = self.call(self.policy(projects=[project]))
        self.assertIn('ownership_mismatch', {c['code'] for c in report['checks']})
        self.assertIn('environment_installation_unverified', {c['code'] for c in report['checks']})
        self.assertIn('project_semantics_unverified', {c['code'] for c in report['checks']})

    def test_planned_project_does_not_require_its_environment_or_outputs(self):
        project = {'id': 'later', 'path': 'later', 'purpose': 'reference', 'status': 'planned',
                   'language': 'chinese', 'environment': 'later/env', 'outputs': ['later/reports'], 'rules': []}
        report = self.call(self.policy(projects=[project]))
        self.assertEqual(report['summary']['deviation'], 0)

    def test_registry_project_identifiers_are_accepted_and_case_unique(self):
        project = {'id': 'QQ_2', 'path': 'QQ', 'purpose': 'reference', 'status': 'planned',
                   'language': 'chinese', 'environment': None, 'outputs': [], 'rules': []}
        report = self.call(self.policy(projects=[project]))
        self.assertEqual(report['summary']['deviation'], 0)
        duplicate = dict(project, id='qq_2', path='other')
        self.call(self.policy(projects=[project, duplicate]), code=2)

    def test_invalid_paths_and_unknown_policy_fields_are_rejected(self):
        for path in ['../outside', 'C:/Windows', '/absolute', 'code/../data', 'NUL', 'bad:stream', 'trail.']:
            with self.subTest(path=path):
                report = self.call(self.policy(paths=[{'path': path, 'kind': 'file', 'state': 'present'}]), code=2)
                self.assertEqual(report['status'], 'error')
        self.call(self.policy(typo=[]), code=2)

    def test_empty_policy_cannot_claim_compliance(self):
        report = self.call(self.policy(), '--strict', code=1)
        self.assertFalse(report['compliant'])
        self.assertEqual(report['checks'][0]['code'], 'no_rules')

    @unittest.skipUnless(os.name == 'nt', 'Windows junction fixture')
    def test_junction_link_source_does_not_read_outside_root(self):
        outside = self.case / 'outside'
        outside.mkdir()
        (outside / 'AGENTS.md').write_text('[rules](../PROJECT_RULES.md)', encoding='utf-8')
        self.file('PROJECT_RULES.md')
        env = dict(os.environ, VERIFY_LINK=str(self.root / 'external'), VERIFY_TARGET=str(outside))
        result = subprocess.run(['powershell', '-NoProfile', '-Command',
                                 'New-Item -ItemType Junction -Path $env:VERIFY_LINK -Target $env:VERIFY_TARGET | Out-Null'],
                                env=env, capture_output=True)
        self.assertEqual(result.returncode, 0)
        report = self.call(self.policy(links=[{'source': 'external/AGENTS.md', 'target': 'PROJECT_RULES.md'}]))
        self.assertEqual(report['checks'][0]['code'], 'not_scanned')


if __name__ == '__main__':
    unittest.main()
