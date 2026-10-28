"""Use Gear's Python scorer contract with an external DSH rollout provider; no Docker required."""
from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path

from .common import BridgeError, digest, file_digest, read_json, staging, write_json
from .exporter import verify_set

SCORER = '''import json
def score(actual, expected, artifacts, *, seed):
    result = json.loads(actual)
    return (result.get("status") == "valid" and type(result.get("passed")) is bool
            and result["passed"] and result.get("source_bundle_digest") == expected["grading"]["source_bundle_digest"]
            and bool(result.get("checks")) and all(c.get("passed") is True for c in result["checks"]))
'''


def export_process_dataset(frozen, destination):
    frozen, destination = Path(frozen), Path(destination)
    manifest = verify_set(frozen)
    if destination.exists():
        old = read_json(destination / "bridge-dataset.json")
        if old["source_digest"] != manifest["digest"]:
            raise BridgeError("dataset_conflict", "数据集已经绑定其他冻结版本")
        return old
    with staging(destination) as temp:
        rows, fixtures = [], {}
        for task in manifest["tasks"]:
            tid = task["task_id"]
            asset = frozen / "tasks" / tid
            public = asset / "environment/initial"
            attachments = []
            for path in sorted(public.rglob("*")):
                if path.is_file():
                    name = "resources/" + tid + "/" + path.relative_to(public).as_posix()
                    dest = temp / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, dest)
                    attachments.append(name)
            grading = read_json(asset / "tests/grading.json")
            expected = {"protocol": "evotrace-python-grader-v1", "grading": grading,
                        "tests_tar_base64": base64.b64encode((asset / "tests/tests.tar.gz").read_bytes()).decode()}
            instruction = (asset / "instruction.md").read_text(encoding="utf-8").replace("Work in /app.", "Work in the current repository.")
            rows.append({"id": tid, "instruction": instruction, "expected": expected,
                         "family": task["family"], "split": task["split"], "attachments": attachments})
            positive = {"status": "valid", "passed": True, "source_bundle_digest": grading["source_bundle_digest"],
                        "checks": [{"passed": True}]}
            fixtures[tid] = [{"actual": json.dumps(positive), "score": 1},
                             {"actual": json.dumps({**positive, "passed": False}), "score": 0}]
        (temp / "tasks.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
        (temp / "scorer.py").write_text(SCORER, encoding="utf-8", newline="\n")
        write_json(temp / "benchmark.yaml", {"version": 1, "source": "tasks.jsonl", "template": "python",
            "scorer": {"entrypoint": "scorer.py:score", "version": "1", "randomness": {"seed": 7},
                       "cost": {"model_requests": 0}, "artifacts": []}, "fixtures": fixtures})
        write_json(temp / "bridge-dataset.json", {"version": 1, "source_digest": manifest["digest"],
                    "tasks_digest": file_digest(temp / "tasks.jsonl"), "scorer_digest": file_digest(temp / "scorer.py"),
                    "execution": "trusted-local-dsh-process", "model_requests": 0})
    return read_json(destination / "bridge-dataset.json")
