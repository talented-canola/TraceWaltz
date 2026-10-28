import tempfile
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evotrace_bridge.common import BridgeError, remove_tree
from evotrace_bridge.gateway import RequestLedger


class BudgetAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parents[1] / '.test-work'
        self.parent.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=self.parent))

    def tearDown(self):
        remove_tree(self.root, self.parent)

    def test_explicit_increase_preserves_previous_calls_and_records_authorization(self):
        ledger = RequestLedger(self.root, limit=5)
        ledger.reserve()
        ledger.reserve()
        update = ledger.authorize_limit(10, reason='user approved increased allowance', expected_previous=5)
        self.assertEqual(update['previous_limit'], 5)
        self.assertEqual(update['request_limit'], 10)
        resumed = RequestLedger(self.root, limit=10)
        self.assertEqual(resumed.summary()['reserved_requests'], 2)
        self.assertEqual(resumed.summary()['remaining_requests'], 8)
        self.assertEqual(resumed.authorizations()[-1]['reason'], 'user approved increased allowance')

    def test_restart_can_read_existing_authorized_limit_without_changing_it(self):
        RequestLedger(self.root, limit=7).reserve()
        self.assertEqual(RequestLedger(self.root).summary()['limit'], 7)
        with self.assertRaises(BridgeError):
            RequestLedger(self.root, limit=8)

    def test_conflicting_or_unexplained_authorization_is_refused(self):
        ledger = RequestLedger(self.root, limit=5)
        with self.assertRaises(BridgeError):
            ledger.authorize_limit(10, reason='', expected_previous=5)
        with self.assertRaises(BridgeError):
            ledger.authorize_limit(10, reason='authorized', expected_previous=4)
        self.assertEqual(RequestLedger(self.root, limit=5).summary()['limit'], 5)

    def test_interrupted_request_remains_reserved_and_unknown(self):
        ledger = RequestLedger(self.root, limit=2)
        ledger.reserve()
        self.assertEqual(ledger.mark_interrupted_before('9999-01-01T00:00:00+00:00', reason='confirmed process exit'), 1)
        self.assertEqual(ledger.summary()['reserved_requests'], 1)
        self.assertEqual(ledger.summary()['unknown_outcomes'], 1)
        self.assertEqual(ledger.mark_interrupted_before('9999-01-01T00:00:00+00:00', reason='repeat inspection'), 0)
        ledger.reserve()
        with self.assertRaises(BridgeError):
            ledger.reserve()


if __name__ == '__main__':
    unittest.main()
