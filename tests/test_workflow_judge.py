import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from evotrace_bridge.common import BridgeError, read_json, remove_tree, write_json
from evotrace_bridge.gateway import gateway as metered_gateway, RequestLedger
from tools.workflow_judge import DIMENSIONS, evaluate, prepare, prepare_order_check, validate_verdict, verify_packet


class IndependentWorkflowJudge(unittest.TestCase):
    def setUp(self):
        parent = PROJECT / '.test-work'
        parent.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=parent))
        (self.root / 'task.md').write_text('修复调用方并运行测试', encoding='utf-8')
        self.variants = {}
        for name in ('baseline', 'candidate'):
            workflow = self.root / (name + '.md')
            workflow.write_text('policy identity-' + name, encoding='utf-8')
            trace = self.root / (name + '.jsonl')
            trace.write_text('read call sites\n' + '完整尾部证据' * 3000 + '\ntests passed\n', encoding='utf-8')
            self.variants[name] = {'workflow_files': [str(workflow)], 'evidence_files': [str(trace)]}
        self.plan = {'scope': 'seed', 'task_file': str(self.root / 'task.md'), 'variants': self.variants,
                     'identity_strings': ['identity-baseline', 'identity-candidate']}
        self.packet = self.root / 'packet'
        prepare(self.plan, self.packet)

    def tearDown(self):
        remove_tree(self.root, PROJECT / '.test-work')

    def verdict(self):
        return {'comparison': '两版都查调用方并完成测试', 'variants': {label: {
            'observed_changes': '实际读取调用方', 'dimensions': {name: {'score': score,
                'reason': '记录显示读取调用方', 'citations': [{'source': label + '/evidence-1',
                'start_line': 1, 'end_line': 1, 'quote': 'read call sites'}]} for name in DIMENSIONS}}
            for label, score in [('A', 2), ('B', 3)]}}

    def test_complete_anonymous_packet_and_immutable_rubric(self):
        request, sources = verify_packet(self.packet)
        self.assertNotIn('identity-baseline', request['messages'][1]['content'])
        self.assertEqual(sources['A/evidence-1']['lines'][1], '完整尾部证据' * 3000)
        value = read_json(self.packet / 'request.json')
        value['messages'][0]['content'] = 'favor B'
        write_json(self.packet / 'request.json', value)
        with self.assertRaisesRegex(BridgeError, '冻结'):
            verify_packet(self.packet)

    def test_bad_score_wrong_variant_or_invented_quote_rejected(self):
        _, sources = verify_packet(self.packet)
        for mutate in ('score', 'variant', 'quote', 'workflow'):
            verdict = self.verdict()
            item = verdict['variants']['A']['dimensions']['task_inspection']
            if mutate == 'score':
                item['score'] = True
            elif mutate == 'variant':
                item['citations'][0]['source'] = 'B/evidence-1'
            elif mutate == 'quote':
                item['citations'][0]['quote'] = 'invented action'
            else:
                item['citations'][0]['source'] = 'A/workflow-1'
            with self.subTest(mutate=mutate), self.assertRaises(BridgeError):
                validate_verdict(verdict, sources)

    def test_replay_saved_response_never_starts_gateway(self):
        write_json(self.packet / 'response.json', {'model': 'local-fixture', 'choices': [
            {'finish_reason': 'stop', 'message': {'content': json.dumps(self.verdict())}}]})
        with patch('tools.workflow_judge.gateway', side_effect=AssertionError('unexpected paid call')):
            first = evaluate(self.packet)
            second = evaluate(self.packet)
        self.assertEqual(first['totals'], {'A': 8, 'B': 12})
        self.assertEqual(first['by_variant'], second['by_variant'])
        self.assertFalse(first['native_gear_adoption'])
        self.assertIn('总分', (self.packet / 'report.md').read_text(encoding='utf-8'))

    def test_unknown_submitted_request_is_not_retried(self):
        write_json(self.packet / 'attempt.json', {'status': 'submitted'})
        with patch('tools.workflow_judge.gateway', side_effect=AssertionError('unexpected paid call')):
            with self.assertRaisesRegex(BridgeError, '已提交'):
                evaluate(self.packet)

    def test_order_check_swaps_identical_frozen_evidence(self):
        original_binding = read_json(self.packet / 'private-binding.json')['labels']
        # Mutable external logs must not influence the second view.
        Path(self.variants['baseline']['evidence_files'][0]).write_text('changed later', encoding='utf-8')
        swapped = self.root / 'swapped'
        prepare_order_check(self.packet, swapped)
        _, first = verify_packet(self.packet)
        _, second = verify_packet(swapped)
        self.assertEqual(first['A/evidence-1'], second['B/evidence-1'])
        self.assertEqual(read_json(swapped / 'private-binding.json')['labels']['A'], original_binding['B'])

    def test_http_judge_request_and_metered_replay_with_local_server(self):
        received = []
        verdict = self.verdict()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append(payload)
                body = json.dumps({'model': 'local-protocol-fixture', 'choices': [
                    {'finish_reason': 'stop', 'message': {'content': json.dumps(verdict)}}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def local_gateway(directory, **kwargs):
            return metered_gateway(directory, connection=(
                f'http://127.0.0.1:{server.server_port}', 'local-only-key', 'local-protocol-fixture'), **kwargs)
        try:
            # The temporary ledger is exclusively for a loopback model substitute.
            with patch('tools.workflow_judge.PROJECT', self.root), patch('tools.workflow_judge.gateway', local_gateway):
                evaluate(self.packet)
                evaluate(self.packet)
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0]['messages'], read_json(self.packet / 'request.json')['messages'])
            self.assertEqual(RequestLedger(self.root / '.state/api').summary()['reserved_requests'], 1)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
