import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evotrace.calibration import calibrate_bundle
from evotrace.errors import EvoTraceError
from evotrace.store import Store
from evotrace_bridge.common import read_json, remove_tree, write_json
from evotrace_bridge.demo import make_sample

PROJECT = Path(__file__).resolve().parents[1]


class CalibrationRecovery(unittest.TestCase):
    def test_completed_trials_are_reused_and_interrupted_trial_requires_explicit_replacement(self):
        parent = PROJECT / '.test-work'
        parent.mkdir(exist_ok=True)
        root = Path(tempfile.mkdtemp(dir=parent))
        try:
            sample = make_sample(root / 'source', 'recovery-task')
            store = Store(root / 'calibration-store')
            calls = []
            def attempt(**kwargs):
                directory = kwargs['attempt_dir']
                directory.mkdir(parents=True, exist_ok=True)
                calls.append(directory)
                if len(calls) == 2:
                    raise RuntimeError('simulated process interruption')
                (directory / 'solution.patch').write_bytes(b'')
                record = {'passed': len(calls) == 1, 'score': 1 if len(calls) == 1 else 0,
                          'reference_equivalent': False, 'solution_patch_id': None}
                write_json(directory / 'attempt.json', record)
                return record
            options = dict(store=store, solver=object(), trials=2, target_passes=1, max_rounds=1)
            with patch('evotrace.calibration._run_attempt', side_effect=attempt):
                with self.assertRaises(RuntimeError):
                    calibrate_bundle(sample['bundle'], **options)
                directory = next(store.calibrations.glob('*/*'))
                with self.assertRaises(EvoTraceError):
                    calibrate_bundle(sample['bundle'], resume_from=directory, **options)
                self.assertEqual(len(calls), 2)
                outcome = calibrate_bundle(sample['bundle'], resume_from=directory, replace_interrupted=True, **options)
                self.assertEqual((outcome.status, outcome.pass_count), ('calibrated', 1))
                self.assertEqual(len(calls), 3)
                self.assertTrue((calls[1] / 'recovery.json').exists())
                self.assertNotEqual(calls[1], calls[2])
                repeated = calibrate_bundle(sample['bundle'], resume_from=directory, **options)
                self.assertEqual(repeated.calibration_id, outcome.calibration_id)
                self.assertEqual(len(calls), 3)
        finally:
            remove_tree(root, parent)
