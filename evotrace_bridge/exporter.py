from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
from pathlib import Path

from .common import BridgeError, digest, extract, file_digest, git, identifier, init_git, inventory, locked, pack_tree, read_json, remove_tree, staging, write_json


def _test_path(name):
    p = Path(name)
    return "tests" in p.parts or p.name.startswith("test_") or p.name.endswith("_test.py") or p.name == "conftest.py"


def _restore(bundle, destination, *, reference=False):
    from evotrace.gitops import apply_patch
    extract(bundle / "environment/base.tar.gz", destination)
    init_git(destination)
    patch = bundle / "patches" / ("reference.patch" if reference else "initial.patch")
    apply_patch(destination, patch)
    archive = bundle / "environment" / ("untracked-reference.tar.gz" if reference else "untracked-initial.tar.gz")
    if archive.is_file() and archive.stat().st_size:
        extract(archive, destination)
    hidden = bundle / "patches/hidden-tests.patch"
    if hidden.is_file() and hidden.stat().st_size:
        # Hidden fixtures are private, but their application base is task-specific.
        # Support only direct bundles in v1 rather than guessing a hardening base.
        raise BridgeError("unsupported_hardening", "首版导出直接任务包；加难任务需要另核对测试应用顺序")
    return destination


def verify_export(directory):
    directory = Path(directory)
    manifest = read_json(directory / "bridge-asset.json")
    body = {k: v for k, v in manifest.items() if k != "digest"}
    if digest(body) != manifest.get("digest") or inventory(directory, exclude={"bridge-asset.json"}) != manifest["files"]:
        raise BridgeError("export_changed", str(directory))
    return manifest


def export_bundle(bundle, destination, *, family, split):
    from evotrace.catalog import bundle_sha256
    from . import grading
    bundle, destination = Path(bundle).resolve(), Path(destination)
    family = identifier(family)
    if split not in {"seed", "held_out"}:
        raise BridgeError("invalid_split", split)
    manifest = read_json(bundle / "task.json")
    verifier = read_json(bundle / "verifier.json")
    environment = manifest.get("environment") or {}
    if environment.get("kind") != "python" or environment.get("install_commands") or environment.get("requirements"):
        raise BridgeError("unsupported_environment", "首版支持无外部安装步骤的 Python 任务，保留原包供后续扩展")
    if not verifier.get("commands") or any(not isinstance(c, str) or not c.strip() for c in verifier["commands"]):
        raise BridgeError("missing_verifier", "任务包没有可执行的行为测试")
    source_digest = bundle_sha256(bundle)
    request = {"source_digest": source_digest, "family": family, "split": split,
               "exporter": "evotrace-harbor-v1", "implementation": file_digest(Path(__file__)),
               "grader": file_digest(Path(grading.__file__))}
    with locked(destination.parent, destination.name):
        if destination.exists():
            saved = verify_export(destination)
            if saved["request"] != request:
                raise BridgeError("export_conflict", "导出目标已绑定其他任务、分组或转换规则")
            return saved
        with staging(destination) as temp:
            work = temp / ".prepare"
            initial = _restore(bundle, work / "initial")
            reference = _restore(bundle, work / "reference", reference=True)
            public = temp / "environment/initial"
            public.mkdir(parents=True)
            initial_items = inventory(initial, exclude={".git", "__pycache__"})
            reference_items = inventory(reference, exclude={".git", "__pycache__"})
            for item in initial_items:
                dest = public / item["path"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(initial / item["path"], dest)
            tests = temp / "tests"
            tests.mkdir()
            frozen_tests = [i["path"] for i in reference_items if _test_path(i["path"])]
            if not frozen_tests:
                raise BridgeError("missing_test_files", "无法定位 Python 测试文件；需要显式测试材料适配")
            with tarfile.open(tests / "tests.tar.gz", "w:gz") as archive:
                for name in frozen_tests:
                    archive.add(reference / name, arcname=name, recursive=False)
            initial_files = {i["path"]: i["digest"][7:] for i in initial_items}
            protected = set(verifier.get("protected_paths", [])) | {n for n in initial_files if _test_path(n)}
            config = {"version": 1, "commands": verifier["commands"], "timeout_seconds": verifier.get("timeout_seconds", 600),
                      "require_change": verifier.get("require_change", True), "initial_files": initial_files,
                      "protected_public": {n: initial_files.get(n) for n in sorted(protected)},
                      "source_bundle_digest": source_digest}
            write_json(tests / "grading.json", config)
            shutil.copyfile(Path(grading.__file__), tests / "grade.py")
            (tests / "test.sh").write_text("#!/bin/sh\nset -eu\npython3 /tests/grade.py\n", encoding="utf-8", newline="\n")
            (tests / "test.sh").chmod(0o755)
            base_image = environment.get("base_image", "python:3.11-slim")
            if not isinstance(base_image, str) or not base_image.startswith("python:") or any(c in base_image for c in "\r\n $"):
                raise BridgeError("invalid_image", str(base_image))
            (temp / "environment/Dockerfile").write_text(
                f"FROM {base_image}\nRUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*\n"
                "WORKDIR /app\nCOPY initial/ /app/\nRUN git init -q && git config user.email bridge@example.invalid && "
                "git config user.name Bridge && git add -A && git commit -qm initial\n", encoding="utf-8", newline="\n")
            (tests / "Dockerfile").write_text(f"FROM {base_image}\nCOPY . /tests\nRUN mkdir -p /app /logs/verifier\nWORKDIR /tests\nCMD [\"sleep\", \"infinity\"]\n", encoding="utf-8", newline="\n")
            # Produce a solution delta relative to the actual initial state, not the original HEAD.
            solution_repo = work / "solution"
            shutil.copytree(public, solution_repo)
            init_git(solution_repo)
            ref_paths = {i["path"] for i in reference_items}
            for item in inventory(solution_repo, exclude={".git"}):
                if item["path"] not in ref_paths:
                    (solution_repo / item["path"]).unlink()
            for item in reference_items:
                if _test_path(item["path"]):
                    continue
                dest = solution_repo / item["path"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(reference / item["path"], dest)
            git(solution_repo, "add", "-N", ".")
            solution = temp / "solution"
            solution.mkdir()
            (solution / "answer.patch").write_bytes(git(solution_repo, "diff", "--binary", "--full-index", "HEAD"))
            (solution / "solve.sh").write_text("#!/bin/sh\nset -eu\ncd /app\ngit apply --binary /solution/answer.patch\n", encoding="utf-8", newline="\n")
            (solution / "solve.sh").chmod(0o755)
            (temp / "instruction.md").write_text(manifest["task"].strip() + "\n\nWork in /app. Modify the code and run the visible tests.\n", encoding="utf-8", newline="\n")
            (temp / "task.toml").write_text(
                'version = "1.0"\nartifacts = ["/app"]\n[metadata]\n'
                f"family = {json.dumps(family)}\nsplit = {json.dumps(split)}\n"
                '[agent]\ntimeout_sec = 600\n[verifier]\ntimeout_sec = 660\nenvironment_mode = "separate"\n'
                '[verifier.environment]\nnetwork_mode = "no-network"\n[environment]\nos = "linux"\n', encoding="utf-8", newline="\n")
            remove_tree(work, temp)
            if source_digest != bundle_sha256(bundle):
                raise BridgeError("bundle_changed", "任务包在导出期间改变")
            body = {"version": 1, "request": request, "source_asset_id": manifest["id"],
                    "source_revision": manifest.get("source_revision"), "files": inventory(temp)}
            write_json(temp / "bridge-asset.json", {**body, "digest": digest(body)})
    return verify_export(destination)


def freeze_set(assets, destination, *, set_id):
    """assets is an explicit list of exported task directories; no automatic repartition."""
    import tomllib
    set_id, destination = identifier(set_id), Path(destination)
    entries, families, instructions, problems, sources = [], {}, {}, {}, set()
    for path in map(Path, assets):
        item = verify_export(path)
        metadata = tomllib.loads((path / "task.toml").read_text(encoding="utf-8"))["metadata"]
        family, split = metadata["family"], metadata["split"]
        source = item["request"]["source_digest"]
        instruction = digest((path / "instruction.md").read_text(encoding="utf-8").strip())
        grading = read_json(path / "tests/grading.json")
        problem = digest({"initial": grading["initial_files"], "solution": file_digest(path / "solution/answer.patch")})
        if source in sources:
            raise BridgeError("duplicate_task", "同一任务包不能重复进入任务集")
        if (families.setdefault(family, split) != split or instructions.setdefault(instruction, split) != split
                or problems.setdefault(problem, split) != split):
            raise BridgeError("split_leakage", "同一任务族或相同题目不能跨越优化与留出分组")
        sources.add(source)
        entries.append({"task_id": identifier(path.name), "source_digest": source, "export_digest": item["digest"], "family": family, "split": split})
    if {e["split"] for e in entries} != {"seed", "held_out"} or len({e["task_id"] for e in entries}) != len(entries):
        raise BridgeError("invalid_task_set", "需要不同编号的优化题和独立留出题")
    body = {"version": 1, "set_id": set_id, "tasks": sorted(entries, key=lambda e: e["task_id"])}
    identity = digest(body)
    with locked(destination.parent, destination.name):
        if destination.exists():
            old = verify_set(destination)
            if old["digest"] != identity:
                raise BridgeError("set_frozen", "该任务集已冻结；请使用新编号")
            return old
        with staging(destination) as temp:
            for path in map(Path, assets):
                shutil.copytree(path, temp / "tasks" / path.name)
                verify_export(temp / "tasks" / path.name)
            write_json(temp / "evaluation-set.json", {**body, "digest": identity})
    return verify_set(destination)


def verify_set(directory):
    directory = Path(directory)
    value = read_json(directory / "evaluation-set.json")
    if value.get('format') == 'tracewaltz-native-harbor':
        from .native_export import verify_native_set
        return verify_native_set(directory)
    if digest({k: v for k, v in value.items() if k != "digest"}) != value["digest"]:
        raise BridgeError("set_changed", str(directory))
    for task in value["tasks"]:
        if verify_export(directory / "tasks" / task["task_id"])["digest"] != task["export_digest"]:
            raise BridgeError("set_changed", task["task_id"])
    expected = {e["task_id"] for e in value["tasks"]}
    if {p.name for p in (directory / "tasks").iterdir()} != expected:
        raise BridgeError("set_changed", "冻结后增加或删除了任务")
    return value
