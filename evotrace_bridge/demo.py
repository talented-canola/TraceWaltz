"""Real local Git/Python smoke flow with synthetic DSH events; no LLM or Docker."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from .common import BridgeError, execute, git, init_git, read_json, write_json
from .exporter import export_bundle, freeze_set, _restore
from .grading import grade
from .recording import capture, ingest


def make_sample(root, name="flags", *, dirty_initial=False, operation="add"):
    from evotrace.compiler import compile_session
    from evotrace.store import Store
    root = Path(root)
    repo = root / "repository"
    (repo / "tests").mkdir(parents=True)
    wrong, correct = ("a - b", "a + b") if operation == "add" else ("a + b", "a * b")
    positive, negative = (5, -5) if operation == "add" else (6, 6)
    function = 'add' if operation == 'add' else 'multiply'
    original = f"def {function}(a, b):\n    return {wrong}\n"
    (repo / "calc.py").write_text(original, encoding="utf-8")
    (repo / "tests/test_calc.py").write_text(
        f"import unittest\nfrom calc import {function}\nclass T(unittest.TestCase):\n"
        f"    def test_positive(self): self.assertEqual({function}(2,3),{positive})\n"
        f"    def test_negative(self): self.assertEqual({function}(-2,-3),{negative})\n", encoding="utf-8")
    init_git(repo)
    if dirty_initial:
        (repo / "calc.py").write_text("# existing user change\n" + original, encoding="utf-8")
    capture(repo, root / "before")
    initial = (repo / "calc.py").read_text(encoding="utf-8")
    (repo / "calc.py").write_text(initial.replace(wrong, correct), encoding="utf-8")
    result = execute([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=repo)
    if result.returncode:
        raise BridgeError("sample_failed", result.stderr.decode("utf-8", "replace"))
    capture(repo, root / "after")
    trace = root / "session.jsonl"
    events = [{"type": "session", "version": 0, "id": "synthetic-" + name, "cwd": str(repo.resolve())},
        {"type": "tool/call", "seq": 1, "data": {"callId": "edit-1", "name": "edit", "arguments": {"file_path": "calc.py", "old_string": wrong, "new_string": correct}}},
        {"type": "tool/result", "seq": 2, "data": {"message": {"source": {"callId": "edit-1"}, "content": [{"type": "tool-result", "toolCallId": "edit-1", "content": [{"type": "text", "text": "Edited calc.py"}]}]}}},
        {"type": "tool/call", "seq": 3, "data": {"callId": "tests-1", "name": "bash", "arguments": {"command": "python -m unittest discover -s tests -v"}}},
        {"type": "tool/result", "seq": 4, "data": {"message": {"source": {"callId": "tests-1"}, "content": [{"type": "tool-result", "toolCallId": "tests-1", "content": [{"type": "text", "text": result.stderr.decode("utf-8", "replace")}]}]}}}]
    trace.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")
    imported = ingest(trace=trace, before=root / "before", after=root / "after", task_id=name, run_id=name + "-1",
                      instruction=f"Fix the Python {operation} function for positive and negative integers ({name}).",
                      store_root=root / "store", verification_commands=["python -m unittest discover -s tests -v"])
    bundle, _ = compile_session(imported["session_id"], store=Store(root / "store"))
    return {"root": root, "repo": repo, "trace": trace, "imported": imported, "bundle": bundle, "operation": operation}


def run_demo(output):
    output = Path(output).resolve()
    if output.exists():
        raise BridgeError("demo_exists", "保留已有结果，请换一个输出目录")
    output.mkdir(parents=True)
    samples, assets, observations = [], [], []
    for name, split in [("positive-addition", "seed"), ("negative-addition", "held_out")]:
        # Demo demonstrates mechanics only. These are near-related tasks, not a generalization benchmark.
        sample = make_sample(output / name, name, dirty_initial=True, operation="add" if split == "seed" else "multiply")
        samples.append(sample)
        asset = output / "exports" / name
        export_bundle(sample["bundle"], asset, family=name, split=split)
        assets.append(asset)
        for state in ["base", "reference", "wrong", "tampered", "equivalent"]:
            workspace = output / name / "controls" / state
            _restore(sample["bundle"], workspace, reference=state == "reference")
            if state == "wrong":
                function = 'add' if split == 'seed' else 'multiply'
                (workspace / "calc.py").write_text(f"def {function}(a,b):\n    return a-b\n")
            if state == "equivalent":
                function = 'add' if split == 'seed' else 'multiply'
                (workspace / "calc.py").write_text(f"def {function}(a,b):\n    return " + ("b+a" if split == "seed" else "b*a") + "\n")
            if state == "tampered":
                (workspace / "tests/test_calc.py").write_text("# removed assertions\n")
            report = grade(asset / "tests", workspace, output / "observations" / name / state)
            expected = state in {"reference", "equivalent"}
            observations.append({"task": name, "state": state, "matched": report.get("passed") is expected, "result": report})
    frozen = freeze_set(assets, output / "frozen", set_id="synthetic-layout-demo")
    summary = {"evidence": "real local Python/Git, synthetic DSH events; no model or Docker",
               "all_matched": all(o["matched"] for o in observations), "controls": observations,
               "dataset_digest": frozen["digest"], "generalization_evidence": False,
               "gear_execution": "not_run", "output": str(output)}
    write_json(output / "summary.json", summary)
    if not summary["all_matched"]:
        raise BridgeError("demo_mismatch", str(output / "summary.json"))
    return {k: v for k, v in summary.items() if k != "controls"}
