from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

from .common import BridgeError, digest, execute, gear_digest, identifier, inventory, locked, now, read_json, staging, write_json
from .gear import active_release, target_resources
from .recording import capture, ingest, normalize_trace, restore_snapshot


def bootstrap(target, releases, *, runtime_version):
    """Register the unchanged starting configuration; this is not a Gear adoption."""
    target, releases = Path(target), Path(releases)
    resources = [{"path": i["path"], "digest": gear_digest(base64.b64encode((target / i["path"]).read_bytes()).decode()),
                  "executable": bool((target / i["path"]).stat().st_mode & 0o111)} for i in inventory(target)]
    body = {"version": 2, "adapterRef": {"id": "dsh", "version": "1"},
            "runtimeIdentity": "bootstrap:" + runtime_version, "baseRef": "operator-baseline",
            "mutationPolicyDigest": digest({"allowed": ["prompts", "skills"]}), "resources": resources}
    manifest = {**body, "digest": gear_digest(body)}
    ref = manifest["digest"]
    with locked(releases, "publication"):
        if (releases / "active.json").exists():
            active = active_release(releases)
            if active["config_ref"] != ref:
                raise BridgeError("baseline_exists", "已有发布版本，不能重新初始化覆盖")
            return active
        directory = releases / "versions" / ref[7:]
        with staging(directory) as temp:
            shutil.copytree(target, temp / "target")
            write_json(temp / "manifest.json", manifest)
        target_resources(directory / "target", manifest)
        record = {"version": 1, "config_ref": ref, "directory": str(directory.resolve()),
                  "runtime_identity": manifest["runtimeIdentity"], "previous": None,
                  "published_at": now(), "origin": "operator-baseline", "runtime_version": runtime_version}
        write_json(releases / "active.json", record)
    return record


def write_patch(target, path):
    """Only the immutable public target is included. No credentials or host sandbox changes."""
    preset = (Path(target) / "preset/agent.cordis.yml").resolve().as_uri()
    skills = str((Path(target) / "skills").resolve()).replace("\\", "/")
    path = Path(path)
    path.write_text("- id: skill-filesystem\n  config:\n    customSkillDirs: [" + json.dumps(skills) + "]\n"
                    "- insert:\n    - id: bridge-coding-policy\n      name: '@deepseek-ai/cordis-plugin-include'\n"
                    "      config:\n        path: " + json.dumps(preset, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return path


def rollback(releases, *, reason):
    releases = Path(releases)
    with locked(releases, "publication"):
        active = read_json(releases / "active.json")
        previous = active.get("previous")
        if not previous:
            raise BridgeError("no_previous_release", "没有可回退的发布版本")
        directory = releases / "versions" / previous[7:]
        manifest = read_json(directory / "manifest.json")
        if gear_digest({k: v for k, v in manifest.items() if k != "digest"}) != previous:
            raise BridgeError("previous_release_changed", "上一版配置已改变")
        target_resources(directory / "target", manifest)
        restored = {"version": 1, "config_ref": previous, "directory": str(directory.resolve()),
                    "runtime_identity": manifest["runtimeIdentity"], "previous": None,
                    "published_at": now(), "origin": "rollback", "reason": reason, "failed_ref": active["config_ref"]}
        write_json(releases / "history" / ("rollback-" + digest(restored)[7:23] + ".json"), restored)
        write_json(releases / "active.json", restored)
        return restored


def run_task(config, *, task_id, run_id, instruction, repository, verification_commands=()):
    """Code in a recovered copy, independently verify, then ingest real DSH events."""
    from .dsh import solve
    task_id, run_id = identifier(task_id), identifier(run_id)
    root = Path(config["data_root"])
    runtime = config["coding_runtime"]
    command = runtime["command"]
    directory = root / "runs" / run_id
    with locked(root, run_id):
        request = {"task_id": task_id, "run_id": run_id, "instruction": instruction,
                   "repository": str(Path(repository).resolve()), "verification_commands": list(verification_commands)}
        if (directory / "execution.json").exists():
            old = read_json(directory / "execution.json")
            if old["request"] != request:
                raise BridgeError("run_id_conflict", "同一运行编号绑定其他需求")
            if old["status"] in {"completed", "task_failed"}:
                return old
            raise BridgeError("run_needs_recovery", "这次编码已经开始；检查记录，不自动重新执行")
        directory.mkdir(parents=True, exist_ok=True)
        record = {"version": 1, "request": request, "purpose": "daily", "status": "preparing", "started_at": now()}
        write_json(directory / "execution.json", record)
        try:
            selected = active_release(root / "releases")
        except (BridgeError, OSError, ValueError) as exc:
            selected = rollback(root / "releases", reason=str(exc))
            record["startup_rollback"] = selected
        # The version CLI is local and performs no model request.
        try:
            version = execute([*command, "--version"], timeout=30)
            observed = version.stdout.decode("utf-8", "replace").strip()
            if version.returncode or runtime["version"] not in observed:
                raise BridgeError("dsh_version_mismatch", "编码 DSH 与配置中固定的版本不同")
            capture(Path(repository), directory / "input")
            workspace = restore_snapshot(directory / "input", directory / "workspace")
            first = capture(workspace, directory / "before")
            record.update({"config_ref": selected["config_ref"], "release": selected, "dsh_version": observed,
                           "before_snapshot": first["digest"], "workspace": str(workspace.resolve())})
            write_json(directory / "execution.json", record)
            record["status"] = "running"
            write_json(directory / "execution.json", record)
            target = Path(selected['directory']) / 'target'
            isolated = config.get('execution', {}).get('isolate_daily', False)
            execution_output = directory / 'execution'
            if isolated:
                from .dsh_meta import sandbox_command
                execution_output.mkdir()
                command = sandbox_command(command, candidate=workspace, materials=directory / 'input',
                                          output=execution_output, mutable=True, extra_read=(target,))
            result = solve(workspace=workspace, target=target,
                           output=execution_output, command=command, instruction=instruction,
                           timeout=runtime.get('timeout_seconds', 600), external_isolation=isolated)
            checks = []
            for text in verification_commands:
                if isolated:
                    verify_command = sandbox_command(['/bin/bash', '-c', text], candidate=workspace,
                        materials=directory / 'input', output=execution_output, mutable=True)
                    verify_env = {k: os.environ[k] for k in ('PATH', 'LANG', 'PYTHONUTF8') if k in os.environ}
                    verify_env.update(HOME=str(execution_output / 'home'), TMPDIR='/tmp')
                    checked = subprocess.run(verify_command, cwd=workspace, env=verify_env,
                                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
                else:
                    checked = subprocess.run(text, shell=True, cwd=workspace, env=os.environ.copy(),
                                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
                checks.append({'command': text, 'exit_code': checked.returncode,
                               'output': checked.stdout.decode('utf-8', 'replace')})
            write_json(directory / 'verification.json', {'checks': checks,
                        'passed': all(c['exit_code'] == 0 for c in checks), 'performed': bool(checks)})
            final = capture(workspace, directory / "after", expected_commit=first["repository"]["base_commit"])
            successful = result['status'] == 'completed' and all(c['exit_code'] == 0 for c in checks)
            imported = ingest(trace=result['trace'], before=directory / "before", after=directory / "after", task_id=task_id,
                              run_id=run_id, instruction=instruction, store_root=root / "evotrace",
                              verification_commands=verification_commands, exit_code=0 if successful else 1,
                              source_repository=read_json(directory / 'input/snapshot.json')['repository'])
            record.update({"status": "completed" if successful else "task_failed",
                           "exit_code": result['exit_code'], "dsh_session_id": result['session_id'], 'dsh': result,
                           "model": result['model'], 'gateway_model': result['gateway_model'],
                           "after_snapshot": final["digest"], "ingestion": imported,
                           "finished_at": now()})
        except Exception as exc:
            if getattr(exc, 'code', None) == 'dsh_startup_failed' and selected.get('previous'):
                record['startup_rollback'] = rollback(root / 'releases', reason=str(exc))
            record.update({"status": "needs_recovery" if record["status"] == "running" else "failed",
                           "error": {"code": getattr(exc, "code", type(exc).__name__), "message": str(exc)}, "finished_at": now()})
            write_json(directory / "execution.json", record)
            raise
        write_json(directory / "execution.json", record)
        return record
