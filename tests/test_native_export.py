import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from evotrace_bridge.common import BridgeError, read_json, remove_tree, write_json
from evotrace_bridge.demo import make_sample
from evotrace_bridge.exporter import _restore
from evotrace_bridge.native_export import export_native, verify_native
from evotrace_bridge.native_verifier import grade


class NativeExports(unittest.TestCase):
    def setUp(self):
        parent = PROJECT / '.test-work'
        parent.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=parent))

    def tearDown(self):
        remove_tree(self.root, PROJECT / '.test-work')

    def test_original_verifier_and_dependency_commands_are_retained(self):
        sample = make_sample(self.root / 'source', 'native-source')
        environment = sample['bundle'] / 'environment/environment.json'
        spec = read_json(environment)
        spec['install_commands'] = ['python -m pip install frozen-example==1.0']
        write_json(environment, spec)
        target = self.root / 'export'
        export_native(sample['bundle'], target, family='arithmetic', split='seed')
        self.assertEqual((target / 'tests/source/verifier.py').read_bytes(), (sample['bundle'] / 'verifier.py').read_bytes())
        self.assertIn(spec['install_commands'][0], (target / 'environment/Dockerfile').read_text())
        self.assertNotIn('reference.patch', (target / 'environment/Dockerfile').read_text())
        self.assertFalse((target / 'environment/bootstrap/reference.patch').exists())
        (target / 'tests/source/verifier.py').write_text('changed')
        with self.assertRaises(BridgeError):
            verify_native(target)

    def test_same_upstream_checks_reject_base_accept_reference_and_reject_test_tampering(self):
        sample = make_sample(self.root / 'source', 'native-scoring')
        target = self.root / 'export'
        export_native(sample['bundle'], target, family='arithmetic', split='seed')
        outputs = {}
        for name in ('base', 'reference', 'tampered'):
            candidate = self.root / name
            _restore(sample['bundle'], candidate, reference=name != 'base')
            if name == 'tampered':
                (candidate / 'tests/test_calc.py').write_text('import unittest\n')
            outputs[name] = grade(target / 'tests/source', candidate, self.root / ('result-' + name))
        self.assertEqual(outputs['base']['status'], 'valid')
        self.assertFalse(outputs['base']['passed'])
        self.assertTrue(outputs['reference']['passed'])
        self.assertFalse(outputs['tampered']['passed'])
        self.assertEqual(outputs['tampered']['score'], 0)
        self.assertIn('checks', outputs['base']['original_report'])
        self.assertTrue((self.root / 'result-base/command-outputs.json').exists())

    @unittest.skipIf(os.name == 'nt', 'Container dependency layout is Linux')
    def test_preserved_private_virtualenv_is_used_after_candidate_upload(self):
        sample = make_sample(self.root / 'source', 'dependency-scoring')
        target = self.root / 'export'
        export_native(sample['bundle'], target, family='dependency', split='seed')
        candidate = self.root / 'candidate'
        _restore(sample['bundle'], candidate, reference=True)
        with (candidate / 'calc.py').open('a') as stream:
            stream.write('\nimport tracewaltz_fixture_dependency\nassert tracewaltz_fixture_dependency.value == 37\n')
        dependencies = self.root / 'trusted-dependencies'
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(dependencies / '.venv')], check=True)
        python = dependencies / '.venv/bin/python'
        site = subprocess.check_output([str(python), '-c', 'import sysconfig;print(sysconfig.get_path("purelib"))'], text=True).strip()
        (Path(site) / 'tracewaltz_fixture_dependency.py').write_text('value = 37\n')
        result = grade(target / 'tests/source', candidate, self.root / 'logs', dependency_root=dependencies)
        self.assertEqual(result['status'], 'valid')
        self.assertTrue(result['passed'], result)

    def test_functional_full_score_does_not_override_original_acceptance(self):
        sample = make_sample(self.root / 'source', 'strict-scoring')
        target = self.root / 'export'
        export_native(sample['bundle'], target, family='arithmetic', split='seed',
            behavior={'components': [{'id': 'functional', 'weight': 1, 'command': 'python -m unittest discover -s tests -v'}]})
        spec = read_json(target / 'tests/source/verifier.json')
        spec['commands'].append('python -c "raise SystemExit(1)"')
        write_json(target / 'tests/source/verifier.json', spec)
        candidate = self.root / 'candidate'
        _restore(sample['bundle'], candidate, reference=True)
        logs = self.root / 'logs'
        result = grade(target / 'tests/source', candidate, logs)
        self.assertEqual(result['score'], 1)
        self.assertFalse(result['passed'])
        self.assertEqual(read_json(logs / 'reward.json')['strict_pass'], 0)


if __name__ == '__main__':
    unittest.main()
