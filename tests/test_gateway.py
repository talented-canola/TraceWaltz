import concurrent.futures
import json
import sys
import tempfile
import unittest
from pathlib import Path
PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from evotrace_bridge.common import BridgeError, remove_tree
from evotrace_bridge.gateway import RequestLedger


class GatewayTests(unittest.TestCase):
    def test_concurrent_reservations_do_not_exceed_persisted_limit(self):
        parent = PROJECT / ".test-work"
        parent.mkdir(exist_ok=True)
        root = Path(tempfile.mkdtemp(dir=parent))
        try:
            ledger = RequestLedger(root, limit=5)
            def reserve(_):
                try:
                    return ledger.reserve()
                except BridgeError:
                    return None
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                accepted = [v for v in pool.map(reserve, range(12)) if v]
            self.assertEqual(len(accepted), 5)
            self.assertEqual(len(set(accepted)), 5)
            restarted = RequestLedger(root, limit=5)
            self.assertEqual(restarted.summary()["remaining_requests"], 0)
            with self.assertRaises(BridgeError):
                restarted.reserve()
            with self.assertRaises(BridgeError):
                RequestLedger(root, limit=6)
        finally:
            remove_tree(root, parent)
