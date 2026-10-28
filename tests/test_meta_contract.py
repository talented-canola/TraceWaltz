import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from evotrace_bridge.common import BridgeError, inventory, remove_tree, write_json
from evotrace_bridge.meta_contract import candidate_changes, meta_result


class MetaContracts(unittest.TestCase):
    def setUp(self):
        parent = PROJECT / '.test-work'
        parent.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=parent))

    def tearDown(self):
        remove_tree(self.root, PROJECT / '.test-work')

    def test_multiple_native_resource_types_are_allowed(self):
        before = inventory(self.root)
        for name in ['preset/agent.cordis.yml', 'plugins/check.js', 'skills/parse/SKILL.md', 'workflows/check.js']:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('candidate material', encoding='utf-8')
        self.assertEqual(len(candidate_changes(before, self.root)), 4)

    def test_dependency_and_unrecognized_root_changes_are_refused(self):
        before = inventory(self.root)
        (self.root / 'package.json').write_text('{}')
        with self.assertRaises(BridgeError):
            candidate_changes(before, self.root)

    def test_meta_cannot_claim_an_unprovided_task(self):
        file = self.root / 'result.json'
        write_json(file, {'declined': False, 'summary': 'read and edited configuration',
                         'hypotheses': ['inspect edge types'], 'clusters': [{'task_ids': ['private-task']}]})
        with self.assertRaises(BridgeError):
            meta_result(file, task_ids={'public-task'})


if __name__ == '__main__':
    unittest.main()
