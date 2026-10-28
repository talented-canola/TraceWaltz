import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

STAGE = Path(__file__).resolve().parents[1]
PROJECT = STAGE
sys.path.insert(0, str(PROJECT))
from evotrace_bridge.common import BridgeError, write_json

spec = importlib.util.spec_from_file_location('evotrace_bridge.staged_meta_contract', STAGE / 'evotrace_bridge/meta_contract.py')
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)

with patch.dict(sys.modules, {'evotrace_bridge.meta_contract': contract}):
    runner_spec = importlib.util.spec_from_file_location('evotrace_bridge.staged_dsh_meta', STAGE / 'evotrace_bridge/dsh_meta.py')
    runner = importlib.util.module_from_spec(runner_spec)
    runner_spec.loader.exec_module(runner)


class CompletedModelResult(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=PROJECT / '.test-work')
        self.path = Path(self.directory.name) / 'result.json'
        self.allowed = {'headers', 'retry-backoff'}

    def tearDown(self):
        self.directory.cleanup()

    def fixture(self):
        return json.loads((PROJECT / 'tests/fixtures/diagnosis-with-affected-tasks.json').read_text(encoding='utf-8'))

    def test_actual_completed_diagnosis_is_normalized_without_rewriting_model_file(self):
        original = self.fixture()
        write_json(self.path, original)
        raw = self.path.read_bytes()
        actual = contract.meta_result(self.path, task_ids=self.allowed)
        self.assertEqual(actual['summary'], original['summary'])
        self.assertEqual(actual['hypotheses'], original['hypotheses'])
        self.assertEqual(actual['clusters'][0]['task_ids'], original['clusters'][0]['affected_tasks'])
        self.assertEqual(actual['clusters'][0]['affected_tasks'], original['clusters'][0]['affected_tasks'])
        self.assertEqual(len(actual['format_normalizations']), 1)
        self.assertEqual(self.path.read_bytes(), raw)

    def test_alias_does_not_authorize_a_private_or_unknown_task(self):
        value = self.fixture()
        value['clusters'][0]['affected_tasks'] = ['private-task']
        write_json(self.path, value)
        with self.assertRaisesRegex(BridgeError, '未授权任务'):
            contract.meta_result(self.path, task_ids=self.allowed)

    def test_disagreeing_aliases_are_rejected_without_guessing(self):
        value = self.fixture()
        value['clusters'][0]['task_ids'] = ['headers']
        write_json(self.path, value)
        with self.assertRaisesRegex(BridgeError, '冲突'):
            contract.meta_result(self.path, task_ids=self.allowed)

    def test_malformed_task_references_return_contract_error(self):
        value = self.fixture()
        value['clusters'][0]['affected_tasks'] = [{'task': 'headers'}]
        write_json(self.path, value)
        with self.assertRaisesRegex(BridgeError, '未授权任务'):
            contract.meta_result(self.path, task_ids=self.allowed)

    def test_canonical_groups_and_ungrouped_results_keep_the_original_contract(self):
        value = {'summary': 'Investigated original evidence.', 'declined': False,
                 'hypotheses': ['Replace the stopping instruction.'], 'clusters': [{'task_ids': ['headers']}]}
        write_json(self.path, value)
        self.assertEqual(contract.meta_result(self.path, task_ids=self.allowed), value)
        del value['clusters']
        write_json(self.path, value)
        self.assertEqual(contract.meta_result(self.path, task_ids=self.allowed)['clusters'], [])

    def test_supplied_example_covers_the_missing_field_and_exact_task_scope(self):
        schema = contract.result_contract(self.allowed)
        group = schema['properties']['clusters']['items']
        self.assertEqual(group['required'], ['task_ids'])
        self.assertEqual(set(group['properties']['task_ids']['items']['enum']), self.allowed)
        example = contract.result_example(self.allowed)
        write_json(self.path, example)
        self.assertEqual(contract.meta_result(self.path, task_ids=self.allowed), example)

    def test_protocol_and_complete_feedback_reach_the_model_input_builder(self):
        parent = self.path.parent / 'targets/anchor'
        (parent / 'prompts').mkdir(parents=True)
        (parent / 'prompts/system.md').write_text('Existing policy', encoding='utf-8')
        operation = self.path.parent / 'operation'
        operation.mkdir()
        feedback = 'complete feedback ' * 2000
        request = {'partition': 'seed', 'operation': {'kind': 'diagnose_seed',
            'candidate_id': 'anchor', 'task_ids': sorted(self.allowed)},
            'evidence': [{'task_id': 'headers', 'feedback': feedback}]}
        context = {'targets': str(parent.parent), 'tasks': [{'id': v} for v in sorted(self.allowed)]}
        configuration = {'dsh_command': ['unexecuted-test-runtime']}
        class InputCaptured(Exception):
            pass
        captured = {}
        def capture(**kwargs):
            captured.update(kwargs)
            raise InputCaptured()
        with patch.object(runner, 'sandbox_command', return_value=['unexecuted-test-runtime']), \
                patch.object(runner, 'solve', side_effect=capture):
            with self.assertRaises(InputCaptured):
                runner.propose(request, context, configuration, operation)
        inputs = json.loads((operation / 'sandbox/input/operation.json').read_text(encoding='utf-8'))
        self.assertEqual(inputs['result_schema'], contract.result_contract(self.allowed))
        self.assertEqual(inputs['result_example'], contract.result_example(self.allowed))
        self.assertEqual(set(inputs['mutation_roots']), {'preset', 'prompts', 'plugins', 'skills', 'workflows'})
        self.assertIn(feedback, captured['instruction'])
        self.assertIn('result-schema.json/result-example.json', captured['instruction'])
        self.assertEqual(json.loads((operation / 'sandbox/input/result-schema.json').read_text(encoding='utf-8')),
                         inputs['result_schema'])


if __name__ == '__main__':
    unittest.main()
