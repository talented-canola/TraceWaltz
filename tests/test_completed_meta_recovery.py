import importlib.util
import tempfile
import unittest
from pathlib import Path

from evotrace_bridge.common import BridgeError

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('completed_meta_recovery', PROJECT / 'tools/recover-completed-meta.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class CompletedMetaRecovery(unittest.TestCase):
    def test_actual_alias_shape_retains_evidence_and_does_not_rewrite_original(self):
        raw = {'summary': 'Changed prompt after tool investigation.', 'declined': False,
               'clusters': [{'affected_tasks': ['headers'], 'observed_behavior': 'No tool calls.'}]}
        value, mappings = recovery.normalize_groups(raw, {'headers'})
        self.assertEqual(value['clusters'][0]['task_ids'], ['headers'])
        self.assertEqual(value['clusters'][0]['observed_behavior'], 'No tool calls.')
        self.assertNotIn('task_ids', raw['clusters'][0])
        self.assertEqual(len(mappings), 1)

    def test_alias_conflict_is_not_guessed(self):
        with self.assertRaisesRegex(BridgeError, '冲突'):
            recovery.normalize_groups({'clusters': [{'affected_tasks': ['headers'], 'task_ids': ['retry']}]}, {'headers', 'retry'})

    def test_alias_does_not_authorize_heldout_or_malformed_references(self):
        for tasks in [['private-task'], [{'task': 'headers'}], 'headers']:
            with self.subTest(tasks=tasks), self.assertRaisesRegex(BridgeError, '无效|授权'):
                recovery.normalize_groups({'clusters': [{'affected_tasks': tasks}]}, {'headers'})

    def test_unrelated_result_failure_is_not_silently_recovered(self):
        with self.assertRaisesRegex(BridgeError, '不存在'):
            recovery.normalize_groups({'clusters': []}, {'headers'})

    def test_changed_configuration_is_allowed_only_inside_native_mutation_roots(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / '.test-work') as root:
            parent, candidate = Path(root) / 'parent', Path(root) / 'candidate'
            for p in [parent, candidate]:
                (p / 'prompts').mkdir(parents=True)
                (p / 'prompts/system.md').write_text('Original', encoding='utf8')
            (candidate / 'prompts/system.md').write_text('Fixed policy', encoding='utf8')
            self.assertEqual(recovery.check_candidate(candidate, parent, 'generate_candidate'), ['prompts/system.md'])
            with self.assertRaisesRegex(BridgeError, '只读诊断'):
                recovery.check_candidate(candidate, parent, 'diagnose_seed')
            (candidate / 'scorer.py').write_text('pass', encoding='utf8')
            with self.assertRaisesRegex(BridgeError, '越过'):
                recovery.check_candidate(candidate, parent, 'generate_candidate')

    def test_no_actual_edit_cannot_be_claimed_as_a_generated_candidate(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / '.test-work') as root:
            parent, candidate = Path(root) / 'parent', Path(root) / 'candidate'
            parent.mkdir(); candidate.mkdir()
            with self.assertRaisesRegex(BridgeError, '没有实际'):
                recovery.check_candidate(candidate, parent, 'generate_candidate')


if __name__ == '__main__':
    unittest.main()
