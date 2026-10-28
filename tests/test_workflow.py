import os
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import nullcontext
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from evotrace_bridge.common import BridgeError, read_json, remove_tree, write_json
from evotrace_bridge.demo import make_sample
from evotrace_bridge.exporter import export_bundle, freeze_set
from evotrace_bridge.runtime import bootstrap
from evotrace_bridge.workflow import prepare_optimization, produce


class WorkflowContracts(unittest.TestCase):
    def setUp(self):
        parent = PROJECT / '.test-work'
        parent.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=parent))

    def tearDown(self):
        remove_tree(self.root, PROJECT / '.test-work')

    def test_produce_refuses_no_behavioral_verification_before_api(self):
        with self.assertRaisesRegex(BridgeError, '测试命令'):
            produce({}, repository=self.root, task_id='x', run_id='y', instruction='fix',
                    verification_commands=[], family='a', split='seed')

    @unittest.skipIf(os.name == 'nt', 'Gear preparation runs on Linux; exercised in WSL')
    def test_prepared_job_binds_dataset_baseline_and_profile_paths(self):
        assets = []
        for name, operation, split in [('addition', 'add', 'seed'), ('multiplication', 'multiply', 'held_out')]:
            sample = make_sample(self.root / name, name, operation=operation)
            asset = self.root / 'assets' / name
            export_bundle(sample['bundle'], asset, family=name, split=split)
            assets.append(asset)
        frozen = self.root / 'frozen'
        manifest = freeze_set(assets, frozen, set_id='contracts')
        initial = bootstrap(PROJECT / 'target', self.root / 'data/releases', runtime_version='test-only')
        config = {'data_root': str(self.root / 'data'), 'coding_runtime': {'command': ['unused'], 'version': 'test-only'}}
        job, request = prepare_optimization(config, frozen, 'job-1')
        self.assertEqual(request['dataset_digest'], manifest['digest'])
        self.assertEqual(request['baseline_ref'], initial['config_ref'])
        self.assertEqual(prepare_optimization(config, frozen, 'job-1'), (job, request))
        profile = read_json(job / 'profiles/coding-target.json')
        self.assertEqual(profile['configuration']['state_root'], str(job / 'provider'))
        self.assertNotIn('.job-', profile['configuration']['state_root'])
        self.assertEqual(read_json(job / 'gear-project/gear.yaml')['validation'], {'mode': 'holdout'})
        config['blocked_source_digests'] = [manifest['tasks'][0]['source_digest']]
        with self.assertRaisesRegex(BridgeError, '退役'):
            prepare_optimization(config, frozen, 'job-retired')

    def test_multiplication_fixture_names_the_same_function_as_its_request(self):
        import ast
        sample = make_sample(self.root / 'multiplication', 'multiply-contract', operation='multiply')
        names = {node.name for node in ast.parse((sample['repo'] / 'calc.py').read_text()).body
                 if isinstance(node, ast.FunctionDef)}
        self.assertIn('multiply', names)
        exported = self.root / 'multiply-export'
        export_bundle(sample['bundle'], exported, family='multiplication', split='held_out')
        self.assertIn('multiply', (exported / 'instruction.md').read_text())
        self.assertIn('from calc import multiply', (exported / 'environment/initial/tests/test_calc.py').read_text())

    def test_completed_production_reuses_checked_bundle_without_rebuilding(self):
        sample = make_sample(self.root / 'cached-source', 'cache-source')
        data = self.root / 'cached-data'
        asset = export_bundle(sample['bundle'], data / 'assets/task-a', family='addition', split='seed')
        record = {'status': 'completed', 'config_ref': 'test-config', 'ingestion': sample['imported']}
        receipt = {'run_id': 'run-a', 'session_id': sample['imported']['session_id'], 'config_ref': 'test-config',
                   'bundle': str(sample['bundle']), 'asset_digest': asset['digest']}
        write_json(data / 'runs/run-a/production.json', receipt)
        with patch('evotrace_bridge.workflow.model_access', return_value=nullcontext()), \
             patch('evotrace_bridge.workflow.run_task', return_value=record), \
             patch('evotrace.compiler.compile_session', side_effect=AssertionError('recompiled completed bundle')):
            result = produce({'data_root': str(data)}, repository=sample['repo'], task_id='task-a', run_id='run-a',
                             instruction='same request', verification_commands=['python -m unittest discover -s tests -v'],
                             family='addition', split='seed')
        self.assertEqual(result, receipt)


if __name__ == '__main__':
    unittest.main()
