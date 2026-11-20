from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import storage
import volumeguard as vg
import pr_check

EX = ROOT / 'examples' / 'mariadb'
BASE = {'services': {'db': {'volumes': ['store:/data']}}, 'volumes': {'store': {}}}


def config():
    return json.loads(json.dumps(BASE))


def state(obj):
    return storage.snapshot(json.dumps(obj).encode(), '.', 'moodle')


def changes(obj):
    return storage.compare(state(BASE), state(obj))


class StorageTests(unittest.TestCase):
    def test_changed(self):
        result = vg.analyze_files(EX/'before.yaml', EX/'changed.yaml')
        self.assertEqual(result, [dict(service='db', target='/var/lib/mysql',
                                      before='volume moodle_dbdata', after='volume moodle_dbfresh',
                                      reason='storage source changed')])

    def test_alias(self):
        self.assertEqual(vg.analyze_files(EX/'before.yaml', EX/'safe-rename.yaml'), [])

    def test_explicit_project(self):
        p = config()
        p['name'] = 'other'
        self.assertTrue(changes(p))
        self.assertEqual(storage.snapshot(json.dumps(p).encode(), '.', 'default', 'moodle'), state(BASE))

    def test_external(self):
        p = config()
        p['volumes']['store'] = {'external': True, 'name': 'moodle_store'}
        self.assertEqual(changes(p), [])
        del p['volumes']['store']['name']
        self.assertEqual(changes(p)[0]['after'], 'volume store')

    def test_bind_and_long_syntax(self):
        p, q = config(), config()
        p['services']['db']['volumes'] = ['./state:/data:ro']
        q['services']['db']['volumes'] = [dict(type='bind', source='./db/../state', target='/data')]
        self.assertEqual(state(p), state(q))
        self.assertEqual(changes(q)[0]['after'], 'bind state')

    def test_long_subpath(self):
        p = config()
        p['services']['db']['volumes'] = [dict(type='volume', source='store', target='/data',
                                              volume={'subpath': 'new'})]
        self.assertIn('subpath new', changes(p)[0]['after'])

    def test_driver_options_not_printed(self):
        p = config()
        p['volumes']['store'] = {'driver_opts': {'device': 'private_value'}}
        result = changes(p)
        self.assertIn('driver', result[0]['reason'])
        self.assertNotIn('private_value', vg.report(result, 'json'))

    def test_mount_removed_service_removed_and_new(self):
        p = config()
        del p['services']['db']['volumes']
        self.assertIn('mount removed', changes(p)[0]['reason'])
        p['services'] = {}
        self.assertIn('service removed', changes(p)[0]['reason'])
        self.assertEqual(storage.compare({}, state(BASE)), [])

    def test_anonymous(self):
        p = config()
        p['services']['db']['volumes'] = ['/data']
        self.assertIn('anonymous', changes(p)[0]['after'])

    def test_image_only_change(self):
        p = config()
        p['services']['db']['image'] = 'mariadb:11.4'
        self.assertEqual(changes(p), [])

    def test_unresolved_inputs(self):
        for field in ('include', 'volumes_from', 'extends', 'variable', 'undefined', 'duplicate-target'):
            with self.subTest(field=field), self.assertRaises(storage.InputError):
                p = config()
                if field == 'include':
                    p[field] = []
                elif field in ('extends', 'volumes_from'):
                    p['services']['db'][field] = 'other'
                elif field == 'variable':
                    p['volumes']['store']['name'] = '${DEPLOY_VOLUME}'
                elif field == 'duplicate-target':
                    p['services']['db']['volumes'] *= 2
                else:
                    p['volumes'] = {}
                state(p)

    def test_rejected_yaml(self):
        for y in (b'', b"services: {}\nservices: {}", b'x: &x [*x]', b'a: [', b'x: !!python/name:os.system'):
            with self.subTest(y=y), self.assertRaises(storage.InputError):
                storage.snapshot(y, '.', 'moodle')

    def test_cli(self):
        for dest, code in [('changed.yaml', 1), ('safe-rename.yaml', 0), ('absent.yaml', 2)]:
            result = subprocess.run([sys.executable, ROOT/'volumeguard.py', EX/'before.yaml', EX/dest,
                                     '--format', 'json'], capture_output=True)
            self.assertEqual(result.returncode, code)
            self.assertEqual(json.loads(result.stdout)['status'], ['clear', 'review', 'error'][code])


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args]).decode().strip()

    def commit(self, source):
        path = self.repo/'compose.yaml'
        if source:
            path.write_bytes((EX/source).read_bytes())
        elif path.exists():
            path.unlink()
        self.git('add', '-A')
        self.git('commit', '--allow-empty', '-qm', 'Fixture')
        return self.git('rev-parse', 'HEAD')

    def test_file_lifecycle(self):
        empty, first, removed = self.commit(None), self.commit('before.yaml'), self.commit(None)
        compare = lambda a,b: vg.analyze_git(self.repo, a, b, 'compose.yaml')
        self.assertEqual(compare(empty, first), [])
        self.assertIn('removed', compare(first, removed)[0]['reason'])
        with self.assertRaises(storage.InputError):
            compare(empty, removed)

    def test_action_entry(self):
        base, head = self.commit('before.yaml'), self.commit('changed.yaml')
        event = self.repo/'event.json'
        event.write_text(json.dumps({'pull_request': {'number': 1, 'base': {'sha': base},
                                    'head': {'repo': {'full_name': 'example/demo'}}}}))
        env = dict(os.environ, GITHUB_EVENT_PATH=str(event), GITHUB_SHA=head,
                   GITHUB_REPOSITORY='example/demo', VG_FILE='compose.yaml',
                   VG_REPO=str(self.repo), GITHUB_STEP_SUMMARY=str(self.repo/'summary'),
                   VG_TOKEN='')
        for blocking in ('true', 'false'):
            proc = subprocess.run([sys.executable, '-I', ROOT/'run_action.py'],
                                  env=dict(env, VG_BLOCK=blocking), capture_output=True)
            self.assertEqual(proc.returncode, int(blocking=='true'), proc.stderr)
        self.assertIn('moodle_dbfresh', (self.repo/'summary').read_text())

    def test_comment_update_or_create(self):
        marker = '<!-- example -->'
        for author, method in [('github-actions[bot]', 'PATCH'), ('another-user', 'POST')]:
            comment = {'id': 15, 'user': {'login': author}, 'body': marker+'\nold'}
            with patch.object(pr_check, 'api', side_effect=[[comment], {}]) as mock:
                pr_check.upsert('https://example.invalid', 1, 'test', marker+'\nnew', marker)
                self.assertEqual(mock.call_args.args[2], method)
