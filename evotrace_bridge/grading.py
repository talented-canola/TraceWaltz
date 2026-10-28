"""Self-contained private Harbor grader, also usable on explicitly trusted local samples."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

IGNORED = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv"}


def files(root):
    found = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if IGNORED.intersection(rel.parts):
            continue
        if path.is_symlink():
            raise ValueError("symlink in candidate: " + rel.as_posix())
        if path.is_file():
            if path.stat().st_nlink != 1:
                raise ValueError("hardlink in candidate: " + rel.as_posix())
            found[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def unpack(archive, root):
    with tarfile.open(archive) as tar:
        for entry in tar.getmembers():
            if entry.name.startswith("/") or ".." in entry.name.split("/") or not (entry.isfile() or entry.isdir()):
                raise ValueError("unsafe private archive")
        tar.extractall(root, filter="data")


def grade(private, candidate, output, *, scratch=None):
    private, candidate, output = Path(private), Path(candidate), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    # A failed regrade must never leave a successful reward behind.
    (output / "reward.txt").unlink(missing_ok=True)
    started = time.monotonic()
    try:
        spec = json.loads((private / "grading.json").read_text(encoding="utf-8"))
        if not spec["commands"]:
            raise ValueError("no behavioral commands")
        actual = files(candidate)
        protected = spec["protected_public"]
        tampered = [p for p, expected in protected.items() if actual.get(p) != expected]
        changed = actual != spec["initial_files"]
        checks = [{"name": "protected tests unchanged", "passed": not tampered, "paths": tampered},
                  {"name": "candidate changed code", "passed": changed or not spec["require_change"]}]
        # Reconstruct from candidate files, then overlay frozen private tests. No candidate .git is trusted.
        with tempfile.TemporaryDirectory(prefix="bridge-grade-", dir=scratch or output) as temp:
            workspace = Path(temp)
            for name in actual:
                dest = workspace / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(candidate / name, dest)
            unpack(private / "tests.tar.gz", workspace)
            environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
            environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
            for command in spec["commands"]:
                try:
                    result = subprocess.run(command, shell=True, cwd=workspace, env=environment,
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            timeout=spec["timeout_seconds"], check=False)
                    code, text = result.returncode, result.stdout.decode("utf-8", "replace")[-12000:]
                except subprocess.TimeoutExpired:
                    code, text = 124, "test timeout"
                checks.append({"name": command, "passed": code == 0, "exit_code": code, "output": text})
        passed = all(c["passed"] for c in checks)
        result = {"status": "valid", "passed": passed, "score": int(passed), "checks": checks,
                  "source_bundle_digest": spec["source_bundle_digest"]}
    except Exception as exc:
        result = {"status": "scoring_failed", "score": None, "reason": type(exc).__name__ + ": " + str(exc)}
    result["duration_ms"] = round((time.monotonic() - started) * 1000)
    (output / "bridge-result.json").write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    if result["status"] == "valid":
        (output / "reward.txt").write_text(str(result["score"]), encoding="ascii")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--private", type=Path, default=Path("/tests"))
    parser.add_argument("--candidate", type=Path, default=Path("/app"))
    parser.add_argument("--output", type=Path, default=Path("/logs/verifier"))
    args = parser.parse_args()
    result = grade(args.private, args.candidate, args.output)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "valid" else 2


if __name__ == "__main__":
    raise SystemExit(main())
