from __future__ import annotations

import base64
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from evotrace_bridge.common import BridgeError, gear_digest, read_json, remove_tree, write_json
from evotrace_bridge.gear import active_release, publish, verify_gear_export
from evotrace_bridge.runtime import rollback


def sealed(body):
    return {**body, "digest": gear_digest(body)}


def fixture(root):
    """Gear-shaped protocol fixture, not evidence of a real Gear/model execution."""
    run_id = "run-fixture"
    run = root / ".gear/runs" / run_id
    exported = root / ".gear/exports" / run_id
    (exported / "target/prompts").mkdir(parents=True)
    data = "Read the repository and verify the change.\n"
    (exported / "target/prompts/system.md").write_bytes(data.encode())
    resource = {"path": "prompts/system.md", "digest": gear_digest(base64.b64encode(data.encode()).decode()), "executable": False}
    common = {"version": 2, "adapterRef": {"id": "dsh", "version": "1"}, "runtimeIdentity": "test-runtime",
              "baseRef": "base", "mutationPolicyDigest": "policy"}
    original = sealed({**common, "resources": [{**resource, "digest": gear_digest("original")}]})
    target = sealed({**common, "resources": [resource]})
    config = {"target": {"adapter": "dsh"}}
    spec = {"validationMode": "holdout", "heldOutTaskIds": ["holdout"], "seedTasks": [{"id": "seed"}],
            "budget": {"repetitions": 1}, "anchor": {"id": "anchor", "digest": original["digest"], "parent_id": None},
            "benchmarkDigest": "benchmark", "configDigest": gear_digest(config)}
    cells = lambda task: [{"candidate_id": cid, "task_id": task, "repetition": 0, "status": "valid", "score": 1} for cid in ["anchor", "candidate"]]
    gate = {"status": "accepted", "reason": "fixture"}
    body = {"version": 1, "id": run_id, "spec": spec, "specDigest": gear_digest(spec), "status": "completed",
            "gate": gate, "nomineeId": "candidate", "championId": "candidate",
            "candidates": [{"id": "candidate", "parent_id": "anchor", "digest": target["digest"]}],
            "seedEvidence": cells("seed"), "heldOutEvidence": cells("holdout")}
    write_json(run / "journal.json", sealed(body))
    write_json(run / "project.json", config)
    write_json(run / "target-manifests/anchor.json", original)
    write_json(run / "target-manifests/candidate.json", target)
    write_json(exported / "manifest.json", target)
    write_json(exported / "report.json", {"run_id": run_id, "retained": "candidate", "nominee": "candidate", "status": "completed",
        "gate": gate, "validation_mode": "independent-held-out"})
    return run_id


class GearReleaseTests(unittest.TestCase):
    def setUp(self):
        scratch = PROJECT / ".test-work"
        scratch.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=scratch))
        self.run = fixture(self.root / "project")

    def tearDown(self):
        remove_tree(self.root, PROJECT / ".test-work")

    def journal(self, change):
        path = self.root / "project/.gear/runs" / self.run / "journal.json"
        saved = read_json(path)
        saved.pop("digest")
        change(saved)
        saved["specDigest"] = gear_digest(saved["spec"])
        write_json(path, sealed(saved))

    def test_accepted_export_is_published_idempotently(self):
        releases = self.root / "releases"
        first = publish(self.root / "project", self.run, releases)
        second = publish(self.root / "project", self.run, releases)
        self.assertEqual(first, second)
        self.assertEqual(active_release(releases)["config_ref"], first["config_ref"])

    def test_rejected_and_shared_set_results_cannot_publish(self):
        self.journal(lambda j: j["gate"].update(status="rejected"))
        with self.assertRaises(BridgeError):
            publish(self.root / "project", self.run, self.root / "releases")
        self.journal(lambda j: (j["gate"].update(status="accepted"), j["spec"].update(validationMode="research")))
        with self.assertRaises(BridgeError):
            verify_gear_export(self.root / "project", self.run)

    def test_missing_or_duplicate_cells_reject(self):
        self.journal(lambda j: j["heldOutEvidence"].pop())
        with self.assertRaisesRegex(BridgeError, "缺少完整"):
            verify_gear_export(self.root / "project", self.run)

    def test_changed_target_or_report_reject(self):
        path = self.root / "project/.gear/exports" / self.run / "target/prompts/system.md"
        path.write_text("unmeasured mutation")
        with self.assertRaises(BridgeError):
            publish(self.root / "project", self.run, self.root / "releases")

    def test_publication_compares_previous_pointer(self):
        with self.assertRaisesRegex(BridgeError, "发布版本已经变化"):
            publish(self.root / "project", self.run, self.root / "releases", expected_previous="unknown")

    def test_gear_canonical_matches_original(self):
        try:
            from gear.canonical import digest as original
        except ImportError:
            self.skipTest("isolated Gear Python environment required")
        for value in [True, 2, -0.0, 0.01, {"中文": [2, False, None], "x": "😀"}]:
            self.assertEqual(gear_digest(value), original(value))


if __name__ == "__main__":
    unittest.main()
