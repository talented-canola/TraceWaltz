import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evotrace_bridge.common import BridgeError, remove_tree
from evotrace_bridge.cycle import run_cycle

PROJECT = Path(__file__).resolve().parents[1]


class CycleRecovery(unittest.TestCase):
    def setUp(self):
        parent = PROJECT / '.test-work'
        parent.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=parent))
        self.config = {'data_root': str(self.root), 'execution': {'backend': 'hitch'}}

    def tearDown(self):
        remove_tree(self.root, PROJECT / '.test-work')

    def test_completed_stage_is_not_reexecuted_and_changed_plan_is_refused(self):
        plan = {'coding_tasks': [{'task_id': 'example'}]}
        with patch('evotrace_bridge.cycle.produce', return_value={'status': 'produced'}) as produce:
            first = run_cycle(self.config, plan, cycle_id='cycle')
            self.assertEqual(first['status'], 'completed')
            self.assertEqual(run_cycle(self.config, plan, cycle_id='cycle'), first)
            self.assertEqual(produce.call_count, 1)
            with self.assertRaises(BridgeError):
                run_cycle(self.config, {}, cycle_id='cycle')

    def test_uncertain_external_operation_is_not_submitted_again(self):
        plan = {'coding_tasks': [{'task_id': 'example'}]}
        with patch('evotrace_bridge.cycle.produce', side_effect=RuntimeError('interrupted')) as produce:
            with self.assertRaises(RuntimeError):
                run_cycle(self.config, plan, cycle_id='cycle')
            with self.assertRaises(BridgeError):
                run_cycle(self.config, plan, cycle_id='cycle')
            self.assertEqual(produce.call_count, 1)
