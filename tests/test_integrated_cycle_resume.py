import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

STAGE = Path(__file__).resolve().parents[1]
PROJECT = STAGE
sys.path.insert(0, str(PROJECT))
from evotrace_bridge.common import BridgeError, gear_digest, write_json

spec = importlib.util.spec_from_file_location('evotrace_bridge.staged_cycle_recovery', STAGE / 'evotrace_bridge/cycle_recovery.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class SameCycleRecovery(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=PROJECT / '.test-work')
        self.root = Path(self.directory.name)
        self.config = {'data_root': str(self.root / 'state'), 'execution': {'backend': 'hitch'}}
        self.dataset = self.root / 'dataset'
        self.job = self.root / 'state/optimizations/round'
        self.options = {'request_id': 'round', 'dataset': str(self.dataset)}
        write_json(self.job / 'request.json', {'request_id': 'round', 'backend': 'hitch',
            'dataset': str(self.dataset), 'dataset_digest': 'frozen'})
        self.journal = {'id': 'original-run', 'status': 'needs_recovery',
            'gate': {'status': 'not_run', 'reason': 'No nominee'}}
        self.save_journal()

    def tearDown(self):
        self.directory.cleanup()

    def save_journal(self):
        write_json(self.job / 'gear-project/.gear/runs/original-run/journal.json',
            {**self.journal, 'digest': gear_digest(self.journal)})

    def complete(self, config, **kwargs):
        self.assertEqual(kwargs, {'dataset': self.dataset, 'request_id': 'round', 'resume': 'original-run'})
        result = {'request_id': 'round', 'run_id': 'original-run', 'status': 'completed',
            'directory': str(self.job), 'published': None, 'gate': self.journal['gate']}
        self.journal['status'] = 'completed'
        self.save_journal()
        write_json(self.job / 'result.json', result)
        return result

    def test_resume_uses_original_run_then_validates_completed_receipt(self):
        with patch.object(recovery, 'verify_native_set', return_value={'digest': 'frozen'}), \
                patch('evotrace_bridge.workflow.evolve', side_effect=self.complete) as evolve:
            actual = recovery.recover_gear_stage(self.config, self.options)
        self.assertEqual(actual['run_id'], 'original-run')
        evolve.assert_called_once()

    def test_running_native_job_is_not_resubmitted(self):
        self.journal['status'] = 'running'
        self.save_journal()
        with patch.object(recovery, 'verify_native_set', return_value={'digest': 'frozen'}), \
                patch('evotrace_bridge.workflow.evolve') as evolve:
            with self.assertRaisesRegex(BridgeError, '仍在执行'):
                recovery.recover_gear_stage(self.config, self.options)
        evolve.assert_not_called()

    def test_changed_dataset_does_not_start_any_native_action(self):
        with patch.object(recovery, 'verify_native_set', return_value={'digest': 'changed'}), \
                patch('evotrace_bridge.workflow.evolve') as evolve:
            with self.assertRaisesRegex(BridgeError, '题集已变化'):
                recovery.recover_gear_stage(self.config, self.options)
        evolve.assert_not_called()

    def test_completed_receipt_is_reused_without_resume_or_model_action(self):
        expected = self.complete(self.config, dataset=self.dataset, request_id='round', resume='original-run')
        with patch.object(recovery, 'verify_native_set', return_value={'digest': 'frozen'}), \
                patch('evotrace_bridge.workflow.evolve') as evolve:
            actual = recovery.recover_gear_stage(self.config, self.options)
        self.assertEqual(actual, expected)
        evolve.assert_not_called()

    def test_ambiguous_native_runs_do_not_choose_a_convenient_one(self):
        write_json(self.job / 'gear-project/.gear/runs/other-run/journal.json', self.journal)
        with patch.object(recovery, 'verify_native_set', return_value={'digest': 'frozen'}), \
                patch('evotrace_bridge.workflow.evolve') as evolve:
            with self.assertRaisesRegex(BridgeError, '唯一确定'):
                recovery.recover_gear_stage(self.config, self.options)
        evolve.assert_not_called()


if __name__ == '__main__':
    unittest.main()
