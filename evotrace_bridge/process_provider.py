"""Gear's real external-process protocol, backed by DSH and an independent Python grader."""
from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path

from .common import BridgeError, digest, file_digest, identifier, inventory, locked, now, read_json, relative_path, write_json
from .dsh import solve, usage
from .grading import grade
from .recording import normalize_trace


def implementation(configuration):
    root = Path(__file__).parent
    return digest({"protocol": "gear-dsh-local-v1", "files": {p.name: file_digest(p) for p in sorted(root.glob("*.py"))},
                   'trusted_harness': inventory(root.parent / 'harness'),
                   "dsh_version": configuration["dsh_version"], "runtime": configuration["dsh_command"]})


def response(request, *, evidence=(), data=None, candidate=None, attempts=0, calls=None, input_tokens=None, output_tokens=None, provenance=None):
    operation = request["operation"]
    return {"result": {"operation_id": request["operationId"], "kind": operation["kind"], "status": "complete",
                       "candidate_id": operation["candidate_id"], "data": data},
            "candidate": candidate, "evidence": list(evidence), "physicalAttempts": attempts,
            "usage": {"model_requests": calls, "input_tokens": input_tokens, "output_tokens": output_tokens,
                      "tokens": input_tokens + output_tokens if input_tokens is not None and output_tokens is not None else None,
                      "dollars": None}, "provenance": provenance}


def meta_call(prompt):
    before = usage()
    payload = {"model": "deepseek-flash", "messages": [{"role": "system", "content": "You improve reusable coding-agent instructions from seed evidence. Return one JSON object. Never write task-specific solutions or reference answers."},
                {"role": "user", "content": prompt}], "response_format": {"type": "json_object"}, "max_tokens": 2048, "stream": False}
    request = urllib.request.Request(os.environ["DEEPSEEK_BASE_URL"].rstrip("/") + "/chat/completions",
                                    data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", 'Authorization': 'Bearer ' + os.environ['DEEPSEEK_API_KEY']})
    with urllib.request.urlopen(request, timeout=150) as connection:
        result = json.load(connection)
    content = json.loads(result["choices"][0]["message"]["content"])
    after = usage()
    return content, {"calls": after["reserved_requests"] - before["reserved_requests"],
                     "input_tokens": after["reported_tokens"]["prompt_tokens"] - before["reported_tokens"]["prompt_tokens"],
                     "output_tokens": after["reported_tokens"]["completion_tokens"] - before["reported_tokens"]["completion_tokens"]}


def evaluate(request, context, configuration, directory):
    from gear.benchmark.python_scorer import score_python
    tasks = {t["id"]: t for t in context["benchmark"]["tasks"]}
    rows, observed = [], []
    for slot in request["slots"]:
        tid, repeat = slot["taskId"], slot["repetition"]
        task = tasks[tid]
        verifier = context["benchmark"]["verifiers"][tid]
        expected = verifier["expected"]
        if expected.get("protocol") != "evotrace-python-grader-v1":
            raise BridgeError("unsupported_grader", tid)
        target = Path(context["targets"]) / identifier(request["operation"]["candidate_id"])
        trial = directory / (tid + "-" + str(repeat))
        trial.mkdir()
        workspace = trial / "workspace"
        workspace.mkdir()
        prefix = "resources/" + tid + "/"
        for resource in task["resources"]:
            if not resource["path"].startswith(prefix):
                raise BridgeError("invalid_resource", resource["path"])
            name = relative_path(resource["path"][len(prefix):])
            dest = workspace / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            content = base64.b64decode(resource["content_base64"], validate=True)
            import hashlib
            if "sha256:" + hashlib.sha256(content).hexdigest() != resource["digest"]:
                raise BridgeError("resource_changed", resource["path"])
            dest.write_bytes(content)
        # The worker sees only its code directory and public instructions. Private files are not copied there.
        started = time.monotonic()
        result = solve(workspace=workspace, target=target, output=trial / "execution",
                       command=configuration["dsh_command"], instruction=task["instruction"], timeout=configuration.get("task_timeout", 240))
        if result['status'] != 'completed':
            raise BridgeError('coding_execution_failed', 'DSH 未正常结束；保留日志，不把环境失败算成任务零分')
        private = trial / "private"
        private.mkdir()
        write_json(private / "grading.json", expected["grading"])
        (private / "tests.tar.gz").write_bytes(base64.b64decode(expected["tests_tar_base64"], validate=True))
        scoring = grade(private, workspace, trial / "verifier")
        # Execute the exact frozen Gear scorer in addition to the test runner.
        checked = score_python(json.dumps(scoring), expected, verifier["rule"]["scorer"], {})
        valid = scoring["status"] == "valid" and checked.status == "valid"
        score = int(scoring.get("passed") is True) if valid else None
        if valid and float(checked.value) != score:
            raise BridgeError("scorer_disagreement", tid)
        proof = digest({"run_id": request["runId"], "operation_id": request["operationId"], "task": tid,
                        "repetition": repeat, "session": result["session_id"], "scoring": scoring})
        raw = {}
        for contract in request.get("metrics", []):
            values = {"model_requests": result["model_requests"], "pass_rate": score}
            value = values.get(contract["id"])
            raw[contract["id"]] = {"contractDigest": contract["digest"], "unit": contract["unit"],
                "status": "available" if value is not None else "unsupported", "evidenceRefs": [proof],
                **({"value": value} if value is not None else {"reason": "metric not observed by DSH adapter"})}
        # Seed feedback contains no private test bodies or reference patch.
        feedback = {"task_passed": scoring.get("passed"), "model_requests": result["model_requests"],
                    "tool_calls": result["tool_calls"], "checks": [{"name": c["name"], "passed": c["passed"]} for c in scoring.get("checks", [])],
                    "final_report": (trial / "execution/stdout.txt").read_text(encoding="utf-8", errors="replace")}
        parsed = normalize_trace(result['trace'])
        feedback['public_tool_steps'] = [
            {'kind': e['kind'], 'name': e['data'].get('name'),
             **({'arguments': e['data'].get('arguments')} if e['kind'] == 'tool.call'
                else {'output': e['data'].get('text', '')})}
            for e in parsed['events'] if e['kind'] in {'tool.call', 'tool.result'}]
        feedback['verification'] = scoring
        write_json(trial / 'public-feedback.json', feedback)
        rows.append({"candidate_id": request["operation"]["candidate_id"], "task_id": tid, "repetition": repeat,
                     "score": score, "status": "valid" if valid else "scoring_failed", "scope": request["operation"]["scope"],
                     "feedback": json.dumps(feedback, ensure_ascii=False), "raw_metrics": raw})
        observed.append({"task_id": tid, "repetition": repeat, "session_id": result["session_id"],
                         "model_requests": result["model_requests"], "input_tokens": result["input_tokens"],
                         "output_tokens": result["output_tokens"], "duration_ms": round((time.monotonic() - started) * 1000), "proof": proof})
    return response(request, evidence=rows, attempts=len(request["slots"]), calls=sum(o["model_requests"] for o in observed),
                    input_tokens=sum(o["input_tokens"] for o in observed), output_tokens=sum(o["output_tokens"] for o in observed),
                    provenance={"execution": "real DSH + actual DeepSeek API + local Python tests", "trials": observed,
                                "isolation": "trusted synthetic repositories; not a security sandbox"})


def propose(request, context, configuration, directory):
    if configuration.get('meta_runtime') == 'dsh':
        from .dsh_meta import propose as propose_dsh
        return propose_dsh(request, context, configuration, directory)
    operation = request["operation"]
    if request["partition"] != "seed":
        raise BridgeError("heldout_meta_forbidden", "留出任务不能用于提出修改")
    parent_id = identifier(operation.get("parent_id") or operation["candidate_id"])
    parent = Path(context["targets"]) / parent_id
    evidence = [{k: row[k] for k in ("task_id", "score", "feedback")} for row in request["evidence"]]
    public = {"parent_prompt": (parent / "prompts/system.md").read_text(encoding="utf-8"),
              "seed_tasks": context["tasks"], "feedback": evidence, "operation": operation}
    if operation["kind"] == "diagnose_seed":
        prompt = ("Diagnose reusable improvements in correctness or model-request efficiency. "
                  "Return {\"hypotheses\":[\"one specific testable reusable strategy\"]}. "
                  "Use seed feedback only.\n" + json.dumps(public, ensure_ascii=False))
        data, measurements = meta_call(prompt)
        if not isinstance(data.get("hypotheses"), list) or not all(isinstance(v, str) and v.strip() for v in data["hypotheses"]):
            raise BridgeError("invalid_diagnosis", "Meta 没有返回可执行的假设")
        write_json(directory / "meta-result.json", data)
        return response(request, data=data, **measurements, provenance={"kind": "real-deepseek-diagnosis", "partition": "seed"})
    prompt = ("Write a complete replacement system instruction for a coding agent. Return {\"system_prompt\":\"...\",\"reason\":\"...\"}. "
              "Preserve correctness and verification. Reduce redundant model requests where supported by feedback, e.g. combine inspection commands. "
              "Use the public_tool_steps to identify actual redundant reads, retries, or test-discovery mistakes. "
              "Specify an actionable small-repository procedure: one batched initial shell inspection can list the repository and read relevant source, tests and runner configuration together; "
              "then perform the necessary edit and run the correct test command, only making extra inspections or retries when the feedback requires them. "
              "When the task provides a test command, use it; otherwise inspect the runner layout and use explicit discovery paths so zero collected tests is not success. "
              "Do not substitute mental reasoning for required executable checks. "
              "Keep it concise and reusable across repositories. Do not include task-specific code or answers. "
              "The hypothesis is a proposal, not an observed improvement.\n" + json.dumps(public, ensure_ascii=False))
    data, measurements = meta_call(prompt)
    text = data.get("system_prompt")
    if not isinstance(text, str) or not 30 <= len(text) <= 12000:
        raise BridgeError("invalid_proposal", "Meta 提示词为空或过长")
    candidate_id = identifier(operation["candidate_id"])
    candidate = Path(context["targets"]) / candidate_id
    if candidate.exists():
        raise BridgeError("candidate_exists", candidate_id)
    shutil.copytree(parent, candidate)
    prompt_path = candidate / 'prompts/system.md'
    prompt_path.chmod(prompt_path.stat().st_mode | 0o200)
    (candidate / "prompts/system.md").write_text(text + "\n", encoding="utf-8", newline="\n")
    write_json(directory / "meta-result.json", data)
    return response(request, candidate={"id": candidate_id, "parent_id": parent_id, "digest": digest(inventory(candidate))},
                    data={"reason": data.get("reason")}, **measurements,
                    provenance={"kind": "real-deepseek-proposal", "partition": "seed", "changed": ["prompts/system.md"]})


def handle(payload):
    configuration, context, action = payload["configuration"], payload.get("context") or {}, payload["action"]
    identity = implementation(configuration)
    if action == "describe":
        return {"version": 1, "identity": identity, "capabilities": {"seeds": False, "temperature": False, "subsets": True,
            "idempotentSubmission": True, "inspect": True, "cancel": True, "physicalAttemptAccounting": True,
            "hardDollarBudget": False, "generationBudget": False, "evidenceProjection": False}}
    if payload.get("implementationIdentity") != identity:
        raise BridgeError("provider_changed", "连接器代码或运行版本已变化")
    request = payload["request"]
    directory = Path(configuration["state_root"]) / identifier(request["runId"]) / digest(request["operationId"])[7:]
    receipt, marker, fence = directory / "receipt.json", directory / "started.json", directory / "cancelled.json"
    directory.mkdir(parents=True, exist_ok=True)
    if action == "inspect":
        if receipt.exists():
            saved = read_json(receipt)
            if saved["request_digest"] != digest(request):
                raise BridgeError("operation_conflict", "运行编号对应不同请求")
            return {"state": "complete", "receipt": saved["receipt"]}
        return {"state": "unknown" if marker.exists() or fence.exists() else "not_submitted"}
    if action == "cancel":
        write_json(fence, {"cancelled_at": now(), "operation_id": request["operationId"]})
        if marker.exists() and not receipt.exists():
            import psutil
            owner = read_json(marker)
            try:
                worker = psutil.Process(owner["pid"])
                if abs(worker.create_time() - owner["create_time"]) < 0.01 and worker.pid != os.getpid():
                    children = worker.children(recursive=True)
                    for child in reversed(children):
                        child.kill()
                    worker.kill()
                    psutil.wait_procs([*children, worker], timeout=15)
                    if worker.is_running():
                        raise BridgeError("cancellation_unconfirmed", "执行进程尚未停止")
            except psutil.NoSuchProcess:
                pass
        return {"cancelled": True}
    if action != "submit":
        raise BridgeError("unsupported_operation", action)
    with locked(configuration["state_root"], request["operationId"]):
        if receipt.exists():
            saved = read_json(receipt)
            if saved["request_digest"] != digest(request):
                raise BridgeError("operation_conflict", "重复请求内容不同")
            return saved["receipt"]
        if fence.exists() or marker.exists():
            raise BridgeError("operation_uncertain", "已开始或已取消的操作不能自动重放")
        import psutil
        write_json(marker, {"pid": os.getpid(), "create_time": psutil.Process().create_time(), "request_digest": digest(request), "started": now()})
        write_json(directory / "request.json", payload)
        if request["operation"]["kind"] == "evaluate_seed":
            result = evaluate(request, context, configuration, directory)
        else:
            result = propose(request, context, configuration, directory)
        write_json(receipt, {"request_digest": digest(request), "receipt": result})
        return result


def main():
    try:
        print(json.dumps(handle(json.load(sys.stdin)), ensure_ascii=False, allow_nan=False))
    except Exception as exc:
        print(f"{getattr(exc, 'code', type(exc).__name__)}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
