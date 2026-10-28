import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from evotrace_bridge.common import execute, remove_tree
from evotrace_bridge.dsh_meta import sandbox_command


@unittest.skipIf(os.name == 'nt', 'Linux OS isolation is verified under WSL')
class MetaIsolation(unittest.TestCase):
    def test_candidate_access_and_private_denial_are_enforced_by_the_os(self):
        parent = PROJECT / '.test-work'
        parent.mkdir(exist_ok=True)
        root = Path(tempfile.mkdtemp(dir=parent))
        try:
            candidate, materials, output = [root / n for n in ('candidate', 'input', 'output')]
            for p in (candidate, materials, output):
                p.mkdir()
            (candidate / 'policy.txt').write_text('original')
            (materials / 'evidence.txt').write_text('public seed evidence')
            private = root / 'private.txt'
            private.write_text('must not be visible')
            script = '''const fs=require('node:fs');
const [candidate,input,output,secret]=process.argv.slice(1);
let privateDenied=false, evidenceReadOnly=false, candidateReadOnly=false;
try { fs.readFileSync(secret); } catch { privateDenied=true; }
try { fs.writeFileSync(input+'/evidence.txt','bad'); } catch { evidenceReadOnly=true; }
try { fs.writeFileSync(candidate+'/policy.txt','bad'); } catch { candidateReadOnly=true; }
fs.writeFileSync(output+'/observed.json',JSON.stringify({privateDenied,evidenceReadOnly,candidateReadOnly}));'''
            node = PROJECT / '.runtime/node-v24.15.0-linux-x64/bin/node'
            command = sandbox_command([str(node), '-e', script, str(candidate), str(materials), str(output), str(private)],
                                      candidate=candidate, materials=materials, output=output, mutable=False)
            result = execute(command, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
            self.assertEqual(json.loads((output / 'observed.json').read_text()),
                             {'privateDenied': True, 'evidenceReadOnly': True, 'candidateReadOnly': True})
            shell = sandbox_command(['/bin/bash', '-c', 'test ! -e "$1"', 'verify', str(private)],
                                    candidate=candidate, materials=materials, output=output, mutable=True)
            self.assertEqual(execute(shell, timeout=30).returncode, 0)
        finally:
            remove_tree(root, parent)


if __name__ == '__main__':
    unittest.main()
