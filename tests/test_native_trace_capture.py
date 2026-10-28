import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evotrace_bridge.common import BridgeError, remove_tree, write_json
from evotrace_bridge.full_evidence import materialize

PROJECT = Path(__file__).resolve().parents[1]


class NativeTraceCapture(unittest.TestCase):
    def test_hitch_declared_native_parent_and_child_details_are_available(self):
        parent = PROJECT / '.test-work'
        parent.mkdir(exist_ok=True)
        root = Path(tempfile.mkdtemp(dir=parent))
        try:
            run = root / 'hitch/runs/example'
            rows = []
            for name, role, marker in [
                ('trajectory/provider/deepseek-session.jsonl', 'provider_events', 'RAW_PARENT_DETAIL'),
                ('trajectory/provider/deepseek-child-session-1.jsonl', 'provider_events', 'RAW_CHILD_DETAIL'),
                ('trajectory/canonical/app/example/session.jsonl', 'canonical_session', 'CANONICAL_DETAIL')]:
                path = run / name
                path.parent.mkdir(parents=True, exist_ok=True)
                content = json.dumps({'type': 'tool/result', 'detail': marker}) + '\n'
                raw = content.encode('utf-8')
                path.write_bytes(raw)
                rows.append({'path': name, 'role': role, 'bytes': len(raw),
                    'sha256': 'sha256:' + hashlib.sha256(raw).hexdigest()})
            write_json(run / 'trajectory.ref.json', {'schema_version': '2', 'run_id': 'example', 'fidelity': 'provider_native', 'files': rows})
            cell = {'task_id': 'task', 'candidate_id': 'anchor', 'repetition': 0, 'score': 1}
            source = {'run_id': 'example', 'source': {'verifier': {}}, 'observation': {}}
            with patch('evotrace_bridge.full_evidence._bound_sources', return_value=[(cell, source)]):
                actual = materialize({}, {}, {'hitch_root': str(root / 'hitch')}, root / 'materialized')
            content = json.dumps(actual)
            self.assertIn('RAW_PARENT_DETAIL', content)
            self.assertIn('RAW_CHILD_DETAIL', content)
            self.assertIn('CANONICAL_DETAIL', content)
            (run / 'trajectory/provider/deepseek-child-session-1.jsonl').write_text('changed')
            with patch('evotrace_bridge.full_evidence._bound_sources', return_value=[(cell, source)]):
                with self.assertRaisesRegex(BridgeError, '清单不一致'):
                    materialize({}, {}, {'hitch_root': str(root / 'hitch')}, root / 'rejected')
        finally:
            remove_tree(root, parent)


if __name__ == '__main__':
    unittest.main()
