import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from evotrace.store import Store
from evotrace.integration import ROLES, collect_review
from evotrace_bridge.common import remove_tree, write_json


class ReviewExports(unittest.TestCase):
    def setUp(self):
        parent = PROJECT / '.test-work'
        parent.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=parent))
        self.store = Store(self.root / 'evotrace')
        self.store.initialize()
        write_json(self.store.root / 'reviews/token/state.json', {'token': 'token', 'candidate': 'task', 'route': 'reject'})

    def tearDown(self):
        remove_tree(self.root, PROJECT / '.test-work')

    def make_events(self, roles):
        rows = []
        for index, role in enumerate(roles):
            rows.append({'type': 'tool/call', 'data': {'callId': str(index), 'name': role, 'arguments': {'input': 'source evidence'}}})
            rows.append({'type': 'tool/result', 'data': {'message': {'source': {'callId': str(index)},
                'content': [{'type': 'text', 'text': 'complete-report-' + role + ' ' + 'evidence ' * 1500}]}}})
        trace = self.root / 'session.jsonl'
        trace.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
        return trace

    def test_full_reports_and_explicit_rejection_are_preserved(self):
        result = collect_review(self.store, candidate='task', token='token', session_path=self.make_events(ROLES), destination=self.root / 'result')
        self.assertEqual(result['status'], 'reject')
        self.assertTrue(result['roles_completed'])
        self.assertFalse(result['asset_verified'])
        self.assertGreater(len(result['role_reports'][0]['report']), 10000)

    def test_started_or_out_of_order_roles_cannot_be_reported_as_finished(self):
        result = collect_review(self.store, candidate='task', token='token', session_path=self.make_events(ROLES[::-1]), destination=self.root / 'result')
        self.assertEqual(result['status'], 'incomplete')
        self.assertFalse(result['roles_completed'])

    def test_recovery_uses_latest_ordered_reports_and_keeps_previous_reports(self):
        trace = self.make_events((*ROLES, *ROLES[2:]))
        result = collect_review(self.store, candidate='task', token='token', session_path=trace, destination=self.root / 'result')
        self.assertTrue(result['roles_completed'])
        self.assertEqual([r['call_index'] for r in result['role_reports']], [0, 2, 8, 10])
        self.assertEqual(len(result['all_role_reports']), 6)


if __name__ == '__main__':
    unittest.main()
