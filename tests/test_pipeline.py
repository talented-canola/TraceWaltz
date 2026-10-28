from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from evotrace_bridge.common import BridgeError, digest, execute, git, inventory, read_json, remove_tree, write_json
from evotrace_bridge.demo import make_sample
from evotrace_bridge.exporter import _restore, export_bundle, freeze_set, verify_export, verify_set
from evotrace_bridge.grading import grade
from evotrace_bridge.recording import ingest, normalize_trace, verify_snapshot


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = PROJECT / ".test-work"
        scratch.mkdir(exist_ok=True)
        cls.root = Path(tempfile.mkdtemp(dir=scratch))
        cls.sample = make_sample(cls.root / "sample", dirty_initial=True)
        cls.asset = cls.root / "exports/task-a"
        export_bundle(cls.sample["bundle"], cls.asset, family="addition", split="seed")

    @classmethod
    def tearDownClass(cls):
        remove_tree(cls.root, PROJECT / ".test-work")

    def fresh(self, name):
        path = self.root / name
        if path.exists():
            remove_tree(path, self.root)
        return path

    def test_normalized_calls_preserve_pairing(self):
        parsed = normalize_trace(self.sample["trace"])
        self.assertEqual((parsed["tool_calls"], parsed["tool_results"], parsed["pending_calls"]), (2, 2, []))
        self.assertEqual([e["data"]["call_id"] for e in parsed["events"]], ["edit-1", "edit-1", "tests-1", "tests-1"])

    def test_repeat_import_reuses_session_and_conflict_rejects(self):
        root = self.sample["root"]
        args = dict(trace=self.sample["trace"], before=root / "before", after=root / "after", task_id="flags", run_id="flags-1",
                    instruction="Fix the Python add function for positive and negative integers (flags).",
                    store_root=root / "store", verification_commands=["python -m unittest discover -s tests -v"])
        self.assertEqual(ingest(**args)["status"], "existing")
        args["instruction"] = "another task"
        with self.assertRaisesRegex(BridgeError, "同一运行编号"):
            ingest(**args)

    def test_pending_call_is_not_complete(self):
        trace = self.root / "incomplete.jsonl"
        rows = self.sample["trace"].read_text(encoding="utf-8").splitlines()[:-1]
        trace.write_text("\n".join(rows), encoding="utf-8")
        parsed = normalize_trace(trace)
        self.assertEqual(parsed["pending_calls"], ["tests-1"])

    def test_source_identity_survives_separate_execution_workspaces(self):
        from evotrace.compiler import compile_session
        from evotrace.store import Store
        from evotrace.evolution import _same_repository
        source = {'root': str(self.root / 'canonical-source'), 'origin': None}
        manifests = []
        for index in (1, 2):
            sample = make_sample(self.root / ('identity-' + str(index)), 'identity-' + str(index))
            imported = ingest(trace=sample['trace'], before=sample['root'] / 'before', after=sample['root'] / 'after',
                task_id='origin-' + str(index), run_id='origin-' + str(index), instruction='Repair addition.',
                store_root=self.root / 'identity-store', source_repository=source,
                verification_commands=['python -m unittest discover -s tests -v'])
            bundle, _ = compile_session(imported['session_id'], store=Store(self.root / 'identity-store'))
            manifests.append(read_json(bundle / 'task.json'))
        self.assertTrue(_same_repository(*manifests, store=Store(self.root / 'identity-store')))
        self.assertFalse(_same_repository(manifests[0], read_json(Path(sample['bundle']) / 'task.json')))

    def test_orphan_and_duplicate_results_reject(self):
        rows = self.sample["trace"].read_text(encoding="utf-8").splitlines()
        for name, modified in [("orphan", [rows[0], rows[2]]), ("duplicate", rows + [rows[2]])]:
            trace = self.root / (name + ".jsonl")
            trace.write_text("\n".join(modified), encoding="utf-8")
            with self.assertRaises(BridgeError):
                normalize_trace(trace)

    def test_snapshot_mutation_rejects(self):
        directory = self.fresh("changed-snapshot")
        shutil.copytree(self.sample["root"] / "before", directory)
        (directory / "changes.patch").write_text("changed")
        with self.assertRaisesRegex(BridgeError, str(directory.name)):
            verify_snapshot(directory)

    def test_export_repeat_and_public_material_boundary(self):
        original = inventory(self.sample["bundle"])
        repeated = export_bundle(self.sample["bundle"], self.asset, family="addition", split="seed")
        self.assertEqual(repeated, verify_export(self.asset))
        self.assertEqual(original, inventory(self.sample["bundle"]))
        public = {i["path"] for i in inventory(self.asset / "environment")}
        self.assertFalse(any("reference" in p or "solution" in p or "grading" in p for p in public))
        self.assertNotIn("a + b", (self.asset / "environment/initial/calc.py").read_text())

    def test_reference_delta_applies_to_dirty_initial_state(self):
        workspace = self.fresh("solution-delta")
        shutil.copytree(self.asset / "environment/initial", workspace)
        from evotrace_bridge.common import init_git
        init_git(workspace)
        git(workspace, "apply", "--binary", str((self.asset / "solution/answer.patch").resolve()))
        self.assertIn("existing user change", (workspace / "calc.py").read_text())
        self.assertIn("a + b", (workspace / "calc.py").read_text())

    def test_actual_grader_controls(self):
        for state in ["base", "reference", "wrong", "tampered", "equivalent", "deleted"]:
            with self.subTest(state=state):
                workspace = self.fresh("grade-" + state)
                _restore(self.sample["bundle"], workspace, reference=state == "reference")
                if state == "wrong":
                    (workspace / "calc.py").write_text("def add(a,b): return a*b\n")
                if state == "equivalent":
                    (workspace / "calc.py").write_text("def add(a,b): return b+a\n")
                if state == "tampered":
                    (workspace / "tests/test_calc.py").write_text("# no assertions\n")
                if state == "deleted":
                    (workspace / "calc.py").unlink()
                result = grade(self.asset / "tests", workspace, self.root / "results" / state)
                self.assertEqual(result["status"], "valid")
                self.assertIs(result["passed"], state in {"reference", "equivalent"})

    def test_scoring_failure_clears_previous_reward(self):
        output = self.root / "failed-grader"
        output.mkdir(exist_ok=True)
        (output / "reward.txt").write_text("1")
        result = grade(self.root / "missing-private", self.asset / "environment/initial", output)
        self.assertEqual(result["status"], "scoring_failed")
        self.assertIsNone(result["score"])
        self.assertFalse((output / "reward.txt").exists())

    def test_missing_commands_and_unsupported_environment_reject(self):
        for case in ["commands", "environment"]:
            directory = self.fresh("bad-bundle-" + case)
            shutil.copytree(self.sample["bundle"], directory)
            if case == "commands":
                spec = read_json(directory / "verifier.json")
                spec["commands"] = []
                write_json(directory / "verifier.json", spec)
            else:
                spec = read_json(directory / "task.json")
                spec["environment"]["requirements"] = ["some-uninstalled-package"]
                write_json(directory / "task.json", spec)
            with self.assertRaises(BridgeError):
                export_bundle(directory, self.root / ("reject-" + case), family="x", split="seed")

    def test_frozen_set_does_not_follow_source_changes(self):
        second = make_sample(self.root / "sample-b", "multiply", operation="multiply")
        asset = self.root / "exports/task-b"
        export_bundle(second["bundle"], asset, family="multiplication", split="held_out")
        frozen = self.root / "dataset"
        expected = freeze_set([self.asset, asset], frozen, set_id="set-v1")
        (asset / "instruction.md").write_text("changed after freeze")
        self.assertEqual(expected, verify_set(frozen))
        (frozen / "tasks/task-a/instruction.md").write_text("changed frozen content")
        with self.assertRaises(BridgeError):
            verify_set(frozen)

    def test_same_problem_cannot_cross_holdout_with_different_ids(self):
        sample = make_sample(self.root / "same-problem", "renamed", dirty_initial=True)
        asset = self.root / "exports/renamed"
        export_bundle(sample["bundle"], asset, family="renamed", split="held_out")
        with self.assertRaisesRegex(BridgeError, "不能跨越"):
            freeze_set([self.asset, asset], self.root / "bad-split", set_id="bad")

    def test_tampered_export_is_not_reused(self):
        copied = self.fresh("copied-export")
        shutil.copytree(self.asset, copied)
        (copied / "tests/test.sh").write_text("exit 0\n")
        with self.assertRaises(BridgeError):
            verify_export(copied)


if __name__ == "__main__":
    unittest.main()
