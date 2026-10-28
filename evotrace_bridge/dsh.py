from __future__ import annotations

import json
import os
import shutil
import urllib.request
from pathlib import Path

from .common import BridgeError, execute, now, write_json
from .recording import normalize_trace
from .runtime import write_patch


def usage():
    url = os.environ["DEEPSEEK_BASE_URL"].rstrip("/") + "/bridge/usage"
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)


def solve(*, workspace, target, output, command, instruction, timeout=240, external_isolation=False):
    workspace, target, output = Path(workspace), Path(target), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    home = output / "dsh-home"
    home.mkdir()
    # The credentials provider accepts an absent store and reads the inherited
    # gateway placeholder. Creating a file on DrvFS would fail its Linux mode check.
    patch = write_patch(target, output / "coding.patch.yml")
    task_file = output / 'task.txt'
    task_file.write_text(instruction, encoding='utf-8')
    startup = Path(__file__).resolve().parents[1] / 'harness/input-file-startup.mjs'
    with patch.open('a', encoding='utf-8') as f:
        f.write('\n- id: headless-startup\n  disabled: true\n- insert:\n    - id: tracewaltz-input-file\n      name: '
                + json.dumps(str(startup), ensure_ascii=False) + '\n      config:\n        path: '
                + json.dumps(str(task_file.resolve()), ensure_ascii=False) + '\n')
    if external_isolation:
        if not any(Path(str(v)).name == 'bwrap' for v in command):
            raise BridgeError('isolation_missing', '外层执行隔离尚未建立')
        with patch.open('a', encoding='utf-8') as f:
            f.write('\n- id: sandbox-policy\n  config: { mode: danger-full-access }\n- id: approval\n  config: { policy: never }\n')
    before = usage()
    environment = {k: v for k, v in os.environ.items()
                   if not any(w in k.upper() for w in ("API_KEY", "AUTH_TOKEN", "ACCESS_TOKEN"))}
    environment['DEEPSEEK_API_KEY'] = os.environ.get('DEEPSEEK_API_KEY', 'gateway-placeholder')
    environment.update(DSH_HOME=str(home.resolve()), DSH_TELEMETRY_DISABLED="1", DSH_PERMISSION_MODE="workspace-write", PYTHONUTF8="1")
    if external_isolation:
        isolated_home = output / 'home'
        isolated_home.mkdir(exist_ok=True)
        environment.update(HOME=str(isolated_home), TMPDIR='/tmp', TEMP='/tmp', TMP='/tmp', LANG='C.UTF-8')
    # Only the gateway placeholder reaches DSH; the real key stays in the gateway process.
    process = execute([*command, "--profile", "headless", "--patch", str(patch.resolve()), "--", "--", 'TraceWaltz task'],
                      cwd=workspace, env=environment, timeout=timeout)
    (output / "stdout.txt").write_bytes(process.stdout)
    (output / "stderr.txt").write_bytes(process.stderr)
    after = usage()
    write_json(output / "process.json", {"exit_code": process.returncode, "usage_before": before, "usage_after": after})
    candidates = list(home.rglob("session*.jsonl*"))
    roots = []
    for trace in candidates:
        parsed = normalize_trace(trace)
        if not parsed["header"].get("parentSession"):
            roots.append((trace, parsed))
    if len(roots) != 1:
        if not roots and process.returncode and after['reserved_requests'] == before['reserved_requests']:
            raise BridgeError('dsh_startup_failed', 'DSH 未创建会话或调用模型，启动输出已保留')
        raise BridgeError("ambiguous_session", f"本次执行产生 {len(roots)} 个根会话")
    trace, normalized = roots[0]
    summary = {"status": "completed" if process.returncode == 0 and not normalized["pending_calls"] else "task_failed",
               "exit_code": process.returncode, "session_id": normalized["header"]["id"],
               "trace": str(trace.resolve()), "model": normalized["model"], "gateway_model": after['model'],
               "tool_calls": normalized["tool_calls"],
               "model_requests": after["reserved_requests"] - before["reserved_requests"],
               "input_tokens": after["reported_tokens"]["prompt_tokens"] - before["reported_tokens"]["prompt_tokens"],
               "output_tokens": after["reported_tokens"]["completion_tokens"] - before["reported_tokens"]["completion_tokens"]}
    write_json(output / "dsh-result.json", summary)
    return summary
