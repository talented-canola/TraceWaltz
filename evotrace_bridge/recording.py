"""Freeze code before/after a run; normalize only caller-selected DSH logs."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .common import BridgeError, digest, file_digest, git, identifier, inventory, locked, now, read_json, staging, write_json


def capture(repo, destination, *, expected_commit=None):
    from evotrace.gitops import capture_metadata, snapshot_untracked, write_patch
    repo, destination = Path(repo).resolve(), Path(destination)
    metadata = capture_metadata(repo)
    if expected_commit and metadata["base_commit"] != expected_commit:
        raise BridgeError("head_changed", "执行期间 Git HEAD 改变，无法沿用原基线生成任务")
    with staging(destination) as temp:
        write_patch(repo, temp / "changes.patch")
        snapshot_untracked(repo, temp / "untracked.tar.gz")
        (temp / "base.tar").write_bytes(git(repo, "archive", "--format=tar", metadata["base_commit"]))
        body = {"version": 1, "repository": metadata, "files": inventory(temp)}
        write_json(temp / "snapshot.json", {**body, "digest": digest(body)})
    return read_json(destination / "snapshot.json")


def verify_snapshot(directory):
    directory = Path(directory)
    value = read_json(directory / "snapshot.json")
    body = {k: v for k, v in value.items() if k != "digest"}
    if digest(body) != value.get("digest") or inventory(directory, exclude={"snapshot.json"}) != value["files"]:
        raise BridgeError("snapshot_changed", str(directory))
    return value


def restore_snapshot(directory, destination):
    from evotrace.gitops import apply_patch, initialize_workspace
    from .common import extract
    directory, destination = Path(directory), Path(destination)
    verify_snapshot(directory)
    if destination.exists() and any(destination.iterdir()):
        raise BridgeError("destination_not_empty", str(destination))
    extract(directory / "base.tar", destination)
    initialize_workspace(destination)
    apply_patch(destination, directory / "changes.patch")
    extract(directory / "untracked.tar.gz", destination)
    return destination


def _rows(path):
    path = Path(path)
    if os.name == "nt" and not str(path).startswith("\\\\?\\"):
        path = Path("\\\\?\\" + str(path.resolve()))
    if path.suffix == ".zstd":
        zstd = shutil.which("zstd")
        if not zstd:
            try:
                import zstandard
                with path.open("rb") as f, zstandard.ZstdDecompressor().stream_reader(f) as reader:
                    text = reader.read().decode("utf-8")
            except ImportError as exc:
                raise BridgeError("zstd_missing", "需要 zstd 或项目环境中的 zstandard") from exc
        else:
            # Passing bytes on stdin also works with Windows paths beyond MAX_PATH.
            result = subprocess.run([zstd, "-dc"], input=path.read_bytes(), capture_output=True, check=False)
            if result.returncode:
                raise BridgeError("trace_decode_failed", str(path))
            text = result.stdout.decode("utf-8")
    else:
        text = path.read_text(encoding="utf-8")
    for line_number, line in enumerate(text.splitlines(), 1):
        if line.strip():
            try:
                value = json.loads(line)
            except (ValueError, TypeError) as exc:
                raise BridgeError("invalid_trace", f"第 {line_number} 行不是 JSON") from exc
            if not isinstance(value, dict):
                raise BridgeError("invalid_trace", f"第 {line_number} 行不是对象")
            yield value


def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(text_content(c) for c in content if isinstance(c, (dict, str)))
    if isinstance(content, dict):
        if content.get("type") in {"text", "input_text", "output_text"}:
            return content.get("text", "")
        if content.get("type") == "tool-result":
            return text_content(content.get("content"))
    return ""


def normalize_trace(path):
    events, calls, results = [], {}, set()
    header = None
    model = {}
    for row in _rows(path):
        kind, data = row.get("type"), row.get("data") or {}
        if kind == "session":
            if header is not None:
                raise BridgeError("mixed_sessions", "一份记录中出现多个会话头")
            if row.get("version") not in {0, 3}:
                raise BridgeError("unsupported_trace_version", "当前转换器支持已核对的 DSH v0 / v3 事件")
            header = row
        event = None
        if kind == "tool/call":
            cid = data.get("callId")
            if not isinstance(cid, str) or not cid or cid in calls:
                raise BridgeError("duplicate_call", "工具调用编号缺失或重复")
            args = data.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError as exc:
                    raise BridgeError("invalid_tool_arguments", cid) from exc
            if not isinstance(args, dict) or not isinstance(data.get("name"), str):
                raise BridgeError("invalid_tool_arguments", cid)
            calls[cid] = data["name"]
            event = {"kind": "tool.call", "data": {"name": data["name"], "arguments": args, "call_id": cid}}
        elif kind == "tool/result":
            message = data.get("message") or {}
            source = message.get("source") or {}
            content = message.get("content") or []
            ids = {item.get("toolCallId") for item in content if isinstance(item, dict) and item.get("type") == "tool-result"}
            cid = source.get("callId") or data.get("callId")
            if cid not in calls or cid in results or (ids and ids != {cid}):
                raise BridgeError("unmatched_result", f"工具结果无法唯一对应调用：{cid}")
            results.add(cid)
            event = {"kind": "tool.result", "data": {"name": calls[cid], "call_id": cid,
                     "text": text_content(content), "is_error": any(c.get("isError") is True for c in content if isinstance(c, dict))}}
        elif kind in {"user/message", "assistant/message"}:
            message = data.get("message") or data
            # user/message may contain skill catalogs and tool results; only human/model text is retained.
            if (message.get("source") or {}).get("kind") not in {"tool", "harness"}:
                text = text_content(message.get("content"))
                if text:
                    event = {"kind": "message.user" if kind == "user/message" else "message.assistant", "data": {"text": text}}
        elif kind == "request/header":
            config = ((data.get("header") or {}).get("config") or {})
            pair = {k: config.get(k) for k in ("provider", "model")}
            if model and pair != model:
                raise BridgeError("model_changed", "一次编码执行中模型发生变化")
            model = pair
        if event:
            event.update({"timestamp": row.get("time"), "source_seq": row.get("seq")})
            events.append(event)
    if not header or not header.get("id"):
        raise BridgeError("missing_session_header", "缺少 DSH 会话头")
    return {"header": header, "events": events, "model": model,
            "pending_calls": sorted(set(calls) - results), "tool_calls": len(calls), "tool_results": len(results)}


def ingest(*, trace, before, after, task_id, run_id, instruction, store_root, purpose="daily", verification_commands=(), exit_code=0, source_repository=None):
    from evotrace.privacy import redact
    from evotrace.provenance import stamp_source
    from evotrace.store import Store
    from evotrace.miner import mine_store
    task_id, run_id = identifier(task_id), identifier(run_id)
    if purpose not in {"daily", "evaluation"} or not instruction.strip():
        raise BridgeError("invalid_task", "缺少任务正文或运行用途无效")
    before, after = Path(before), Path(after)
    first, last = verify_snapshot(before), verify_snapshot(after)
    if first["repository"]["base_commit"] != last["repository"]["base_commit"]:
        raise BridgeError("head_changed", "开始和结束快照不在同一 Git 基线上")
    normalized = normalize_trace(trace)
    request = {"task_id": task_id, "run_id": run_id, "instruction": instruction, "purpose": purpose,
               "before": first["digest"], "after": last["digest"], "trace": file_digest(trace),
               "commands": list(verification_commands), "exit_code": exit_code}
    repository = first['repository'].copy()
    if source_repository is not None:
        if not isinstance(source_repository, dict) or not isinstance(source_repository.get('root'), str) or not source_repository['root']:
            raise BridgeError('invalid_repository_identity', '来源仓库缺少实际目录')
        request['source_repository'] = source_repository
        # Recovery still needs the reconstructed checkout and its actual commit.
        # Canonical input identity is separate and source-revision-bound.
        repository['source_repository'] = source_repository
    identity = digest(request)
    sid = "dsh-" + digest({"run_id": run_id})[7:31]
    store = Store(Path(store_root))
    target = store.sessions / sid
    with locked(store.root, sid):
        if target.exists():
            saved = read_json(target / "bridge.json")
            if saved["input_digest"] != identity:
                raise BridgeError("run_id_conflict", "同一运行编号对应了不同输入")
            if inventory(target, exclude={"bridge.json"}) != saved["files"]:
                raise BridgeError("session_changed", "已导入的会话内容发生变化")
            return {"session_id": sid, "input_digest": identity, "status": "existing"}
        with staging(target) as temp:
            (temp / "patches").mkdir()
            (temp / "environment").mkdir()
            for source, dest in [(before / "changes.patch", "patches/initial.patch"),
                                 (after / "changes.patch", "patches/final.patch"),
                                 (before / "untracked.tar.gz", "environment/untracked-initial.tar.gz"),
                                 (after / "untracked.tar.gz", "environment/untracked-final.tar.gz")]:
                shutil.copyfile(source, temp / dest)
            events = [{"kind": "message.user", "data": {"text": instruction}, "timestamp": None}]
            # The explicit task is authoritative; exclude historical user turns to prevent mixed-task mining.
            events += [e for e in normalized["events"] if e["kind"] != "message.user"]
            with (temp / "events.jsonl").open("w", encoding="utf-8", newline="\n") as f:
                for event in events:
                    f.write(json.dumps(redact(event), ensure_ascii=False) + "\n")
            trajectory = {"schema_version": "0.1", "session_id": sid, "created_at": now(),
                "source": {"kind": "command", "agent": "dsh", "source_session_id": normalized["header"]["id"]},
                "task": {"text": instruction, "source": "explicit"},
                "repository": {**repository, "reconstruction_confidence": "high"},
                "snapshots": {"initial": {"patch": "patches/initial.patch"}, "final": {"patch": "patches/final.patch"}},
                "verification": {"commands": list(verification_commands)},
                "outcome": {"status": "completed" if exit_code == 0 and not normalized["pending_calls"] else "incomplete", "exit_code": exit_code},
                "bridge": {"task_id": task_id, "run_id": run_id, "purpose": purpose,
                           "model": normalized["model"], "pending_calls": normalized["pending_calls"]}}
            write_json(temp / "trajectory.json", stamp_source(temp, redact(trajectory)))
            write_json(temp / "bridge.json", {"input_digest": identity, "files": inventory(temp), "input": request})
        mine_store(store)
    return {"session_id": sid, "input_digest": identity, "status": "created", "complete": not normalized["pending_calls"] and exit_code == 0}
