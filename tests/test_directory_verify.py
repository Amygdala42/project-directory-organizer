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

    def test_central_environment_uses_id_and_preserves_existing_local_environment(self):
        for environment in ('env/crawler', 'env/crawler/python'):
            with self.subTest(environment=environment):
                for path in ('apps/crawler/reports', environment, 'legacy/.venv'):
                    (self.root / path).mkdir(parents=True, exist_ok=True)
                base = {'purpose': 'code', 'status': 'active', 'language': 'english', 'rules': []}
                projects = [dict(base, id='crawler', path='apps/crawler', environment=environment,
                                 outputs=['apps/crawler/reports']),
                            dict(base, id='legacy', path='legacy', environment='legacy/.venv', outputs=[])]
                before = {str(p.relative_to(self.case)): p.read_bytes() if p.is_file() else None
                          for p in self.case.rglob('*')}
                report = self.call(self.policy(projects=projects), '--strict', code=1)
                after = {str(p.relative_to(self.case)): p.read_bytes() if p.is_file() else None
                         for p in self.case.rglob('*')}
                self.assertEqual(before, after)
                self.assertEqual(report['summary']['deviation'], 0, report['checks'])
                self.assertFalse(report['compliant'])
                unverified = [c['path'] for c in report['checks']
                              if c['code'] == 'environment_installation_unverified']
                self.assertEqual(unverified, [environment, 'legacy/.venv'])
                self.assertTrue(all(c['status'] == 'unverified' for c in report['checks']
                                    if c['code'] == 'environment_installation_unverified'))

    def test_planned_central_environment_is_not_missing(self):
        project = {'id': 'crawler', 'path': 'apps/crawler', 'purpose': 'code', 'status': 'planned',
                   'language': 'english', 'environment': 'env/crawler/python',
                   'outputs': ['apps/crawler/reports'], 'rules': []}
        report = self.call(self.policy(projects=[project]))
        self.assertEqual(report['summary']['deviation'], 0, report['checks'])
        self.assertEqual([c['path'] for c in report['checks'] if c['code'] == 'planned_absent'],
                         ['apps/crawler', 'env/crawler/python', 'apps/crawler/reports'])

    def test_central_environment_cannot_claim_other_id_or_prefix_sibling(self):
        base = {'id': 'crawler', 'path': 'apps/crawler', 'purpose': 'code', 'status': 'planned',
                'language': 'english', 'outputs': [], 'rules': []}
        for environment in ('env', 'env/beta', 'env/beta/python', 'env/crawler2', 'other/env/crawler'):
            with self.subTest(environment=environment):
                report = self.call(self.policy(projects=[dict(base, environment=environment)]))
                self.assertEqual([c['path'] for c in report['checks'] if c['code'] == 'ownership_mismatch'],
                                 [environment])

    def test_central_environment_exception_does_not_allow_central_outputs(self):
        base = {'id': 'crawler', 'path': 'apps/crawler', 'purpose': 'code', 'status': 'planned',
                'language': 'english', 'environment': 'env/crawler', 'rules': []}
        for output in ('env/crawler/reports', 'env/reports', 'apps/beta/reports'):
            with self.subTest(output=output):
                report = self.call(self.policy(projects=[dict(base, outputs=[output])]))
                self.assertEqual([c['path'] for c in report['checks'] if c['code'] == 'ownership_mismatch'],
                                 [output])

    def test_central_environment_cannot_overlap_another_project_root(self):
        base = {'purpose': 'code', 'status': 'planned', 'language': 'english', 'outputs': [], 'rules': []}
        for root in ('env', 'env/crawler', 'env/crawler/nested'):
            with self.subTest(root=root):
                projects = [dict(base, id='crawler', path='apps/crawler', environment='env/crawler'),
                            dict(base, id='beta', path=root, environment=None)]
                report = self.call(self.policy(projects=projects))
                self.assertTrue(any(c['code'] == 'ownership_mismatch' and c['path'] == 'env/crawler'
                                    for c in report['checks']), report['checks'])

    def test_central_environment_reports_shared_or_containing_environment(self):
        base = {'purpose': 'code', 'status': 'planned', 'language': 'english', 'outputs': [], 'rules': []}
        for environment in ('env/crawler', 'env/crawler/python', 'env'):
            with self.subTest(environment=environment):
                projects = [dict(base, id='crawler', path='apps/crawler', environment='env/crawler'),
                            dict(base, id='beta', path='apps/beta', environment=environment)]
                report = self.call(self.policy(projects=projects))
                self.assertTrue(any(c['code'] == 'environment_overlap' and c['path'] == 'env/crawler'
                                    for c in report['checks']), report['checks'])

    def test_central_environment_cannot_overlap_another_project_output(self):
        base = {'purpose': 'code', 'status': 'planned', 'language': 'english', 'rules': []}
        for output in ('env/crawler', 'env/crawler/reports', 'env'):
            with self.subTest(output=output):
                projects = [dict(base, id='crawler', path='apps/crawler', environment='env/crawler', outputs=[]),
                            dict(base, id='beta', path='apps/beta', environment=None, outputs=[output])]
                report = self.call(self.policy(projects=projects))
                self.assertTrue(any(c['code'] == 'environment_overlap' and c['path'] == 'env/crawler'
                                    for c in report['checks']), report['checks'])

    def test_local_environment_cannot_contain_another_project_root(self):
        base = {'purpose': 'code', 'status': 'planned', 'language': 'english', 'outputs': [], 'rules': []}
        projects = [dict(base, id='alpha', path='alpha', environment='alpha/env'),
                    dict(base, id='beta', path='alpha/env/beta', environment=None)]
        report = self.call(self.policy(projects=projects))
        self.assertTrue(any(c['code'] == 'ownership_mismatch' and c['path'] == 'alpha/env'
                            for c in report['checks']), report['checks'])

    def test_environment_ownership_matches_registry_case_and_unicode_keys(self):
        base = {'id': 'crawler', 'purpose': 'code', 'status': 'planned', 'language': 'english',
                'outputs': [], 'rules': []}
        for root, environment in (('apps/crawler', 'ENV/Crawler/python'),
                                  ('apps/caf\u00e9', 'APPS/cafe\u0301/python')):
            with self.subTest(root=root, environment=environment):
                report = self.call(self.policy(projects=[dict(base, path=root, environment=environment)]))
                self.assertEqual(report['summary']['deviation'], 0, report['checks'])

    def test_environment_conflicts_match_registry_case_and_unicode_keys(self):
        base = {'purpose': 'code', 'status': 'planned', 'language': 'english', 'outputs': [], 'rules': []}
        projects = [dict(base, id='crawler', path='apps/crawler', environment='env/crawler/caf\u00e9'),
                    dict(base, id='beta', path='ENV/Crawler/cafe\u0301/nested', environment=None)]
        report = self.call(self.policy(projects=projects))
        self.assertTrue(any(c['code'] == 'ownership_mismatch' and c['path'] == 'env/crawler/caf\u00e9'
                            for c in report['checks']), report['checks'])

    def test_environment_cannot_be_or_contain_its_own_project_root(self):
        base = {'id': 'crawler', 'purpose': 'code', 'status': 'planned', 'language': 'english',
                'outputs': [], 'rules': []}
        for root, environment in (('apps/crawler', 'apps/crawler'),
                                  ('env/crawler', 'env/crawler'),
                                  ('env/crawler/work', 'env/crawler')):
            with self.subTest(root=root, environment=environment):
                report = self.call(self.policy(projects=[dict(base, path=root, environment=environment)]))
                self.assertEqual([c['path'] for c in report['checks'] if c['code'] == 'ownership_mismatch'],
                                 [environment])

    def test_local_environment_cannot_overlap_its_own_outputs(self):
        base = {'id': 'crawler', 'path': 'apps/crawler', 'purpose': 'code', 'status': 'planned',
                'language': 'english', 'environment': 'apps/crawler/env', 'rules': []}
        for output in ('apps/crawler/env', 'apps/crawler/env/reports', 'apps/crawler'):
            with self.subTest(output=output):
                report = self.call(self.policy(projects=[dict(base, outputs=[output])]))
                self.assertTrue(any(c['code'] == 'environment_overlap' and c['path'] == 'apps/crawler/env'
                                    for c in report['checks']), report['checks'])

    def test_nested_local_environment_keeps_ownership_under_central_named_root(self):
        base = {'purpose': 'code', 'status': 'planned', 'language': 'english', 'outputs': [], 'rules': []}
        projects = [dict(base, id='outer', path='env', environment=None),
                    dict(base, id='child', path='env/child', environment='env/child/.venv')]
        for ordered in (projects, projects[::-1]):
            with self.subTest(order=[p['id'] for p in ordered]):
                report = self.call(self.policy(projects=ordered))
                self.assertEqual(report['summary']['deviation'], 0, report['checks'])
                self.assertTrue(any(c['code'] == 'planned_absent' and c['path'] == 'env/child/.venv'
                                    for c in report['checks']))

    def test_project_root_aliases_are_rejected_before_ownership_checks(self):
        base = {'purpose': 'code', 'status': 'planned', 'language': 'english',
                'environment': None, 'outputs': [], 'rules': []}
        projects = [dict(base, id='alpha', path='apps/caf\u00e9'),
                    dict(base, id='beta', path='APPS/cafe\u0301')]
        for ordered in (projects, projects[::-1]):
            with self.subTest(order=[p['id'] for p in ordered]):
                report = self.call(self.policy(projects=ordered), code=2)
                self.assertEqual(report['code'], 'invalid_policy')

    def test_output_cannot_equal_its_own_project_root(self):
        base = {'id': 'crawler', 'purpose': 'code', 'status': 'planned', 'language': 'english',
                'environment': None, 'rules': []}
        for root, output in (('apps/crawler', 'apps/crawler'), ('apps/caf\u00e9', 'APPS/cafe\u0301')):
            with self.subTest(root=root, output=output):
                report = self.call(self.policy(projects=[dict(base, path=root, outputs=[output])]))
                self.assertEqual([c['path'] for c in report['checks'] if c['code'] == 'ownership_mismatch'],
                                 [output])

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
