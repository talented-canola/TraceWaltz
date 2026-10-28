import json
from contextlib import nullcontext
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

STAGING = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGING))
from evotrace.catalog import bundle_sha256
from evotrace.errors import EvoTraceError
from evotrace.integration import AssetService
from evotrace.provenance import file_digest
from evotrace.recorded_experience import products
from evotrace.store import Store
from evotrace.util import read_json, write_json


class Runner:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail
    def run_prompt(self, workspace, prompt, *, timeout):
        self.calls += 1
        if self.fail:
            raise RuntimeError('response lost')
        return {'exit_code': 0, 'stdout': json.dumps({'summary': 'Observed one repository check.',
            'runtime_facts': ['The recorded check passed.'], 'useful_commands': ['python -m unittest'],
            'code_locations': ['policy.py'], 'failure_modes': [], 'do_not_assume': ['Transfer is untested.'],
            'confidence': 'medium'}), 'model': 'local-fixture', 'tool_calls': 0}


class RecordedExperience(unittest.TestCase):
    def setUp(self):
        parent = STAGING / '.test-work'
        parent.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.directory.name)
        self.store = Store(self.root / 'store')
        self.store.initialize()
        self.trace = self.root / 'native.jsonl'
        self.trace.write_text('\n'.join(json.dumps(value) for value in [
            {'type': 'session', 'version': 3, 'id': 'native-source'},
            {'type': 'tool/call', 'data': {'callId': 'call-1', 'name': 'bash',
                'arguments': json.dumps({'command': 'python -m unittest'})}},
            {'type': 'tool/result', 'data': {'message': {'source': {'callId': 'call-1'},
                'content': [{'type': 'text', 'text': 'repository check passed'}]}}},
        ]), encoding='utf-8')
        source = self.store.sessions / 'source'
        write_json(source / 'trajectory.json', {'session_id': 'source', 'source_revision': 'revision-a',
            'source': {'source_session_id': 'native-source'}})
        write_json(source / 'bridge.json', {'input': {'trace': 'sha256:' + file_digest(self.trace)}})
        self.arguments = {'solver_result': {'session_log_path': str(self.trace)},
            'workspace': self.root, 'source': {'session_id': 'source', 'run_id': 'coding'}, 'request_id': 'recorded'}
        self.runner = Runner()

    def tearDown(self):
        self.directory.cleanup()

    def compress(self, runner=None):
        return AssetService(self.store).compress_recording(runner=runner or self.runner, **self.arguments)

    def test_real_capsule_and_parser_replay_preserve_recording(self):
        first = self.compress()
        self.assertTrue(first['compression_accepted'])
        self.assertEqual(first['utility'], 'not_evaluated')
        self.assertEqual(self.compress(), first)
        self.assertEqual(self.runner.calls, 1)
        directory = Path(first['directory'])
        self.assertEqual((directory / 'recording.jsonl').read_bytes(), self.trace.read_bytes())
        self.assertEqual(read_json(directory / 'assessment.json')['capsule_stats']['tool_calls'], 1)
        (directory / 'experience.json').write_text('{}')
        with self.assertRaisesRegex(EvoTraceError, 'artifact changed'):
            self.compress()
        self.assertEqual(self.runner.calls, 1)

    def test_trace_mutation_rejected_before_compression(self):
        self.trace.write_text(self.trace.read_text() + '\n' + json.dumps({'type': 'turn/end'}))
        with self.assertRaisesRegex(EvoTraceError, 'changed after import'):
            self.compress()
        self.assertEqual(self.runner.calls, 0)

    def test_unknown_model_execution_is_not_resubmitted(self):
        runner = Runner(fail=True)
        with self.assertRaises(RuntimeError):
            self.compress(runner)
        with self.assertRaisesRegex(EvoTraceError, 'needs recovery'):
            self.compress(runner)
        self.assertEqual(runner.calls, 1)

    def test_products_keep_unrelated_sources_out_and_check_parent(self):
        self.compress()
        parent = self.store.benchmarks / 'source'
        write_json(parent / 'task.json', {'id': 'source', 'source_revision': 'revision-a', 'source': {'kind': 'command'}})
        same = products(self.store, parent)
        self.assertEqual({row['kind'] for row in same}, {'recorded_experience', 'compressed_experience', 'recorded_experience_assessment'})
        other = self.store.benchmarks / 'other'
        write_json(other / 'task.json', {'id': 'other', 'source_revision': 'revision-a'})
        self.assertEqual(products(self.store, other), [])
        child = self.store.benchmarks / 'child'
        write_json(child / 'task.json', {'id': 'child', 'source_revision': 'child-revision',
            'source': {'parent_id': 'source', 'parent_bundle_sha256': bundle_sha256(parent)}})
        self.assertEqual(len(products(self.store, child)), 3)
        (parent / 'unexpected.txt').write_text('parent changed')
        with self.assertRaisesRegex(EvoTraceError, 'parent source'):
            products(self.store, child)

    def test_bridge_adapts_the_original_trace_and_execution_scope(self):
        from evotrace_bridge.recorded_experience import compress_run
        record = {'status': 'task_failed', 'request': {'task_id': 'task'},
            'ingestion': {'session_id': 'source', 'input_digest': 'input'},
            'config_ref': 'config', 'before_snapshot': 'before', 'after_snapshot': 'after',
            'dsh': {'trace': str(self.trace)}, 'workspace': str(self.root)}
        write_json(self.root / 'runs/coding/execution.json', record)
        with patch('evotrace.calibration.DeepSeekHarnessSolver', return_value=self.runner), \
                patch.object(AssetService, 'compress_recording', return_value={'status': 'captured'}) as compress:
            self.assertEqual(compress_run({'data_root': str(self.root)}, run_id='coding', request_id='experience')['status'], 'captured')
        self.assertEqual(compress.call_args.kwargs['solver_result']['session_log_path'], str(self.trace))
        self.assertEqual(compress.call_args.kwargs['source']['execution_status'], 'task_failed')

    def test_cycle_exposes_recorded_experience_and_reuses_completed_stage(self):
        from evotrace_bridge.cycle import run_cycle
        config = {'data_root': str(self.root), 'execution': {'backend': 'hitch'}}
        plan = {'recorded_experience': [{'run_id': 'coding', 'request_id': 'experience'}]}
        with patch('evotrace_bridge.cycle.model_access', return_value=nullcontext()), \
                patch('evotrace_bridge.recorded_experience.compress_run', return_value={'status': 'completed'}) as compress:
            first = run_cycle(config, plan, cycle_id='recording-cycle')
            self.assertEqual(first['status'], 'completed')
            self.assertEqual(run_cycle(config, plan, cycle_id='recording-cycle'), first)
            self.assertEqual(compress.call_count, 1)

    def test_real_export_keeps_daily_experience_and_its_unvalidated_status(self):
        from evotrace_bridge.common import write_json as bridge_write
        from evotrace_bridge.demo import make_sample
        from evotrace_bridge.native_export import export_native, freeze_native
        from evotrace_bridge.native_optimization import prepare_native_optimization
        seed = make_sample(self.root / 'seed', 'recorded-seed')
        holdout = make_sample(self.root / 'holdout', 'independent-holdout', operation='multiply')
        store = Store(self.root / 'seed/store')
        AssetService(store).compress_recording(runner=self.runner,
            solver_result={'session_log_path': str(seed['trace'])}, workspace=seed['repo'],
            source={'session_id': seed['imported']['session_id']}, request_id='actual-export')
        evidence = AssetService(store).products(seed['bundle'])
        export_native(seed['bundle'], self.root / 'assets/seed', family='addition', split='seed', products=evidence)
        export_native(holdout['bundle'], self.root / 'assets/holdout', family='multiplication', split='held_out')
        freeze_native([self.root / 'assets/seed', self.root / 'assets/holdout'], self.root / 'dataset', set_id='experience-fixture')
        target = self.root / 'target'
        bridge_write(target / 'prompts/example.json', {'fixture': True})
        config = {'data_root': str(self.root / 'state'),
            'execution': {'base_image': 'python:3.11-slim@sha256:' + 'a' * 64},
            'coding_runtime': {'version': '0.1.5-rc.3', 'model': 'local-fixture', 'command': ['/usr/bin/node', 'fixture']}}
        with patch('evotrace_bridge.native_optimization.active_release', return_value={
                'directory': str(self.root), 'config_ref': 'fixture-baseline'}):
            job, _ = prepare_native_optimization(config, self.root / 'dataset', 'daily-experience')
        public = job / 'public-materials/tasks/seed'
        assessment = read_json(public / 'experience/assessments.json')['results'][0]
        self.assertEqual(assessment['utility'], 'not_evaluated')
        self.assertFalse(assessment['comparison_performed'])
        self.assertTrue(list((public / 'experience').glob('*/experience.json')))
        self.assertFalse(list(public.rglob('recording.jsonl')))
        self.assertFalse(list(public.rglob('recorded-experience.json')))


if __name__ == '__main__':
    unittest.main()
