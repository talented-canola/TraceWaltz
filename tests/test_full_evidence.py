import hashlib
import unittest
from unittest.mock import patch

from evotrace_bridge.common import BridgeError
from evotrace_bridge.full_evidence import diagnostic, public_details


class FullDiagnostics(unittest.TestCase):
    def test_all_pages_are_read_and_frozen_digest_is_checked(self):
        content = 'first page\n' + 'x' * 70000
        sha = hashlib.sha256(content.encode()).hexdigest()
        artifact = {'name': 'test-stdout.txt', 'bytes': len(content), 'sha256': sha, 'source_complete': True}
        def page(config, *args):
            offset = int(args[args.index('--offset') + 1])
            part = content[offset:offset + 65536]
            return {'run_id': 'example', 'artifact': artifact,
                    'page': {'offset': offset, 'bytes': len(part), 'text': part,
                             'eof': offset + len(part) == len(content), 'next_offset': offset + len(part)}}
        with patch('evotrace_bridge.full_evidence._hitch', side_effect=page) as read:
            self.assertEqual(diagnostic({}, 'example', 'test-stdout.txt', expected_digest=sha)['text'], content)
            self.assertEqual(read.call_count, 2)
            with self.assertRaises(BridgeError):
                diagnostic({}, 'example', 'test-stdout.txt', expected_digest='a' * 64)

    def test_private_reference_is_removed_without_shortening_public_content(self):
        raw = {'message': 'details' * 10000, 'private_details_ref': '/private', 'nested': [{'private_details_ref': 'secret', 'keep': 1}]}
        public = public_details(raw)
        self.assertEqual(public['message'], raw['message'])
        self.assertEqual(public['nested'], [{'keep': 1}])

    def test_incomplete_source_is_rejected(self):
        with patch('evotrace_bridge.full_evidence._hitch', return_value={
            'run_id': 'example', 'artifact': {'name': 'stdout.txt', 'source_complete': False},
            'page': {'offset': 0, 'text': 'lost', 'eof': True}}):
            with self.assertRaises(BridgeError):
                diagnostic({}, 'example', 'stdout.txt')
