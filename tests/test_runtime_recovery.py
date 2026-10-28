import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

STAGING = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGING))
from evotrace_bridge.common import BridgeError, gear_digest, read_json, write_json
from evotrace_bridge.container_runtime import daemon_environment, ensure_hitch
from evotrace_bridge.cycle import run_cycle
from evotrace_bridge.gear import GearClient


class Recovery(unittest.TestCase):
    def setUp(self):
        parent = STAGING / '.test-work'
        parent.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.directory.name)
        self.config = {'data_root': str(self.root / 'state'), 'execution': {'backend': 'hitch'}}
    def tearDown(self):
        self.directory.cleanup()

    def test_cold_daemon_can_become_ready_after_old_eight_poll_window(self):
        counts = {'status': 0, 'start': 0, 'stop': 0}
        def execute(argv, **kwargs):
            action = argv[argv.index('daemon') + 1]
            counts[action] += 1
            if action == 'start':
                return SimpleNamespace(returncode=1, stdout=b'', stderr=b'not ready yet')
            ready = counts['start'] and counts['status'] > 9
            return SimpleNamespace(returncode=0 if ready else 1,
                stdout=json.dumps({'status': 'running' if ready else 'starting'}).encode(), stderr=b'')
        with patch('evotrace_bridge.container_runtime.__file__', str(self.root / 'project/pkg/container_runtime.py')), \
                patch('evotrace_bridge.container_runtime.execute', side_effect=execute), \
                patch('evotrace_bridge.container_runtime.time.sleep'):
            ensure_hitch(self.config)
        self.assertEqual(counts, {'status': 10, 'start': 1, 'stop': 0})
        self.assertTrue((self.root / 'state/hitch/tracewaltz-environment.json').is_file())

    def test_readiness_expiration_preserves_started_daemon(self):
        started = []
        def execute(argv, **kwargs):
            started.append(argv[argv.index('daemon') + 1])
            return SimpleNamespace(returncode=1, stdout=b'', stderr=b'not ready')
        with patch('evotrace_bridge.container_runtime.__file__', str(self.root / 'project/pkg/container_runtime.py')), \
                patch('evotrace_bridge.container_runtime.execute', side_effect=execute), \
                patch('evotrace_bridge.container_runtime.time.monotonic', side_effect=[0, 121]), \
                patch('evotrace_bridge.container_runtime.time.sleep'):
            with self.assertRaises(BridgeError):
                ensure_hitch(self.config)
        self.assertEqual(started, ['status', 'start', 'status'])

    def test_busy_native_rerun_prevents_daemon_replacement(self):
        actions = []
        def execute(argv, **kwargs):
            actions.append(argv[argv.index('daemon') + 1])
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                'status': 'running', 'scheduler': None, 'eval_scheduler': {'running': 0, 'queued': 0},
                'eval_rerun_scheduler': {'running': 1, 'queued': 0}}).encode(), stderr=b'')
        with patch('evotrace_bridge.container_runtime.__file__', str(self.root / 'project/pkg/container_runtime.py')), \
                patch('evotrace_bridge.container_runtime.execute', side_effect=execute):
            with self.assertRaisesRegex(BridgeError, '正在执行任务'):
                ensure_hitch(self.config)
        self.assertEqual(actions, ['status'])

    def test_long_refine_wait_uses_native_limits_instead_of_one_hour_transport_kill(self):
        client = GearClient(self.root)
        with patch.object(client, 'invoke', return_value={'run_id': 'original-run'}) as invoke:
            client.refine('request')
        invoke.assert_called_once_with('refine', timeout=None)

    def test_volatile_shell_environment_does_not_change_resident_binding_input(self):
        first = {'WSL_INTEROP': '/run/old_interop', 'SHLVL': '3', 'RANDOM_SESSION_ID': 'first',
                 'DEEPSEEK_API_KEY': 'test-placeholder', 'DEEPSEEK_BASE_URL': 'http://gateway', 'PATH': '/old'}
        second = {**first, 'WSL_INTEROP': '/run/new_interop', 'SHLVL': '5', 'RANDOM_SESSION_ID': 'second', 'PATH': '/new'}
        with patch.dict('os.environ', first, clear=True):
            a = daemon_environment(self.root / 'project', self.root / 'hitch')
        with patch.dict('os.environ', second, clear=True):
            b = daemon_environment(self.root / 'project', self.root / 'hitch')
        self.assertEqual(a, b)
        self.assertNotIn('WSL_INTEROP', a)
        self.assertIn('test-placeholder', a.values())

    def finished_gear(self):
        job = Path(self.config['data_root']) / 'optimizations/round'
        dataset = self.root / 'dataset'
        gate = {'status': 'not_run', 'reason': 'No nominee'}
        result = {'request_id': 'round', 'run_id': 'original-run', 'status': 'completed',
                  'directory': str(job), 'published': None, 'gate': gate}
        write_json(job / 'request.json', {'request_id': 'round', 'dataset': str(dataset), 'dataset_digest': 'dataset'})
        write_json(job / 'result.json', result)
        journal = {'id': 'original-run', 'status': 'completed', 'gate': gate}
        write_json(job / 'gear-project/.gear/runs/original-run/journal.json', {**journal, 'digest': gear_digest(journal)})
        return {'optimization': {'dataset': str(dataset), 'request_id': 'round'}}, job

    def interrupted_cycle(self, plan):
        with patch('evotrace_bridge.cycle.evolve', side_effect=RuntimeError('parent interrupted')):
            with self.assertRaises(RuntimeError):
                run_cycle(self.config, plan, cycle_id='cycle')

    def test_verified_completed_native_stage_is_reused_without_model_action(self):
        plan, _ = self.finished_gear()
        self.interrupted_cycle(plan)
        with patch('evotrace_bridge.cycle_recovery.verify_native_set', return_value={'digest': 'dataset'}), \
                patch('evotrace_bridge.cycle.evolve', side_effect=AssertionError('must not resubmit')):
            state = run_cycle(self.config, plan, cycle_id='cycle', resume=True)
        self.assertEqual(state['status'], 'completed')
        self.assertIn('verified', state['stages']['gear']['recovery'])
        self.assertIn('error', state['stages']['gear'])

    def test_running_or_mismatched_native_receipt_cannot_complete_stage(self):
        plan, job = self.finished_gear()
        self.interrupted_cycle(plan)
        path = job / 'gear-project/.gear/runs/original-run/journal.json'
        journal = read_json(path)
        journal['status'] = 'running'
        journal['digest'] = gear_digest({key: value for key, value in journal.items() if key != 'digest'})
        write_json(path, journal)
        with patch('evotrace_bridge.cycle_recovery.verify_native_set', return_value={'digest': 'dataset'}), \
                patch('evotrace_bridge.cycle.evolve', side_effect=AssertionError('must not resubmit')):
            with self.assertRaisesRegex(BridgeError, '尚未完成'):
                run_cycle(self.config, plan, cycle_id='cycle', resume=True)
        self.assertEqual(read_json(Path(self.config['data_root']) / 'cycles/cycle/state.json')['status'], 'needs_recovery')


if __name__ == '__main__':
    unittest.main()
