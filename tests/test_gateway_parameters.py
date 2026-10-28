import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from evotrace_bridge.common import remove_tree
from evotrace_bridge.gateway import gateway


class GatewayParameters(unittest.TestCase):
    def setUp(self):
        parent = PROJECT / '.test-work'
        parent.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=parent))
        self.observed = []
        observed = self.observed
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                observed.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                body = json.dumps({'choices': [{'message': {'content': 'synthetic'}}], 'usage': {'total_tokens': 4}}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        remove_tree(self.root, PROJECT / '.test-work')

    def test_unauthorized_client_cannot_spend_allowance_and_generation_settings_survive(self):
        endpoint = 'http://127.0.0.1:' + str(self.server.server_port)
        with gateway(self.root, limit=3, connection=(endpoint, 'synthetic-upstream-key', 'configured-model')) as service:
            content = {'model': 'requested-model', 'messages': [], 'max_tokens': 8192, 'thinking': {'type': 'enabled'}}
            request = urllib.request.Request(service['url'] + '/chat/completions', data=json.dumps(content).encode())
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 401)
            self.assertEqual(service['ledger'].summary()['reserved_requests'], 0)
            request.add_header('Authorization', 'Bearer ' + service['token'])
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(self.observed[0]['max_tokens'], 8192)
            self.assertEqual(self.observed[0]['thinking'], {'type': 'enabled'})
            self.assertEqual(self.observed[0]['model'], 'configured-model')
            self.assertEqual(service['ledger'].summary()['reserved_requests'], 1)


if __name__ == '__main__':
    unittest.main()
