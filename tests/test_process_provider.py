"""Deterministic crash/retry controls; no model calls are made by these tests."""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from evotrace_bridge.common import BridgeError, digest, remove_tree, write_json
from evotrace_bridge.process_provider import handle, implementation, response
from evotrace_bridge.recording import normalize_trace


class ProcessControls(unittest.TestCase):
    def setUp(self):
        parent = PROJECT / '.test-work'
        parent.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=parent))
        self.config = {'state_root': str(self.root), 'dsh_version': 'test-only', 'dsh_command': ['unused']}
        self.request = {'runId': 'run_controls', 'operationId': 'operation_1',
                        'operation': {'kind': 'diagnose_seed', 'candidate_id': 'anchor'}}
        self.payload = {'configuration': self.config, 'context': {}, 'request': self.request,
                        'action': 'submit', 'implementationIdentity': implementation(self.config)}
        self.operation = self.root / 'run_controls' / digest('operation_1')[7:]

    def tearDown(self):
        remove_tree(self.root, PROJECT / '.test-work')

    def test_completed_operation_retry_reuses_receipt(self):
        receipt = response(self.request, data={'hypotheses': ['test-only']})
        with patch('evotrace_bridge.process_provider.propose', return_value=receipt) as worker:
            self.assertEqual(handle(self.payload), receipt)
            self.assertEqual(handle(self.payload), receipt)
            self.assertEqual(worker.call_count, 1)
        observed = handle({**self.payload, 'action': 'inspect'})
        self.assertEqual(observed, {'state': 'complete', 'receipt': receipt})
        changed = {**self.request, 'partition': 'different'}
        with self.assertRaises(BridgeError):
            handle({**self.payload, 'request': changed})

    def test_crash_does_not_automatically_repeat_model_work(self):
        with patch('evotrace_bridge.process_provider.propose', side_effect=RuntimeError('injected crash')):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                handle(self.payload)
        with patch('evotrace_bridge.process_provider.propose') as worker:
            with self.assertRaisesRegex(BridgeError, '不能自动重放'):
                handle(self.payload)
            worker.assert_not_called()
        self.assertEqual(handle({**self.payload, 'action': 'inspect'})['state'], 'unknown')

    def test_cancel_stops_exact_owned_process_and_fences_replay(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'])
        try:
            write_json(self.operation / 'started.json', {'pid': child.pid,
                'create_time': psutil.Process(child.pid).create_time(), 'request_digest': digest(self.request)})
            self.assertEqual(handle({**self.payload, 'action': 'cancel'}), {'cancelled': True})
            child.wait(timeout=10)
            with self.assertRaises(BridgeError):
                handle(self.payload)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_v3_message_and_tool_result_pairing(self):
        path = self.root / 'v3.jsonl'
        rows = [
            {'type': 'session', 'version': 3, 'id': 'session-v3', 'cwd': str(self.root)},
            {'type': 'request/header', 'data': {'header': {'config': {'provider': 'deepseek', 'model': 'flash'}}}},
            {'type': 'user/message', 'data': {'content': [{'type': 'text', 'text': 'Fix a bug.'}]}},
            {'type': 'tool/call', 'seq': 1, 'data': {'callId': 'c1', 'name': 'read_file', 'arguments': '{"path":"a.py"}'}},
            {'type': 'tool/result', 'seq': 2, 'data': {'message': {'source': {'callId': 'c1'}, 'content': [
                {'type': 'tool-result', 'toolCallId': 'c1', 'content': [{'type': 'text', 'text': 'return 0'}]}]}}},
            {'type': 'assistant/message', 'data': {'message': {'content': [{'type': 'text', 'text': 'Finished.'}]}}},
        ]
        path.write_text('\n'.join(json.dumps(r) for r in rows), encoding='utf-8')
        actual = normalize_trace(path)
        self.assertEqual(actual['tool_calls'], 1)
        self.assertEqual(actual['pending_calls'], [])
        self.assertEqual(actual['events'][0]['data']['text'], 'Fix a bug.')
        self.assertEqual(actual['events'][-1]['data']['text'], 'Finished.')


if __name__ == '__main__':
    unittest.main()
