"""Public Gear CLI transport and read-only verification of its retained exports."""
from __future__ import annotations

import base64
import json
import os
import shutil
from pathlib import Path

from .common import BridgeError, digest, execute, file_digest, gear_digest, identifier, inventory, locked, now, read_json, relative_path, staging, write_json

PINS = {"penguin": "4694d85312da47a2ceb8ef5051ea618cbab0bfe4",
        "gear": "c3809b95ce27bbc9420cc3836cbb692097ff9f1e",
        "hitch": "6385c22388e69a3df0832cd88f21500a525751ee",
        "dsh": "0.1.5-rc.3"}


class GearClient:
    def __init__(self, project, command=("gear",)):
        self.project, self.command = Path(project).resolve(), list(command)

    def invoke(self, *args, timeout=3600):
        try:
            result = execute([*self.command, *args, "--project", str(self.project), "--json"], cwd=self.project, timeout=timeout)
        except FileNotFoundError as exc:
            raise BridgeError("gear_missing", "找不到 Gear；连接器已就绪，需在 Linux 工作环境安装固定版本") from exc
        if result.returncode:
            raise BridgeError("gear_failed", result.stderr.decode("utf-8", "replace")[-3000:])
        try:
            return json.loads(result.stdout)
        except ValueError as exc:
            raise BridgeError("gear_invalid_response", "Gear 未返回单个 JSON 结果") from exc

    def import_set(self, dataset):
        from .exporter import verify_set
        frozen = verify_set(dataset)
        preview = self.invoke("benchmark", "import", str(Path(dataset).resolve() / "tasks"), "--template", "harbor")
        plan = preview.get("plan_id")
        if not isinstance(plan, str) or not plan.startswith("import_"):
            raise BridgeError("gear_invalid_response", "导入预览缺少 plan_id")
        # The returned plan freezes bytes. Check the source once more before applying that exact plan.
        verify_set(dataset)
        applied = self.invoke("benchmark", "import", "--apply", plan)
        return {"set_digest": frozen["digest"], "plan_id": plan, "result": applied,
                "check": self.invoke("benchmark", "check")}

    def refine(self, request_id, *, timeout=None):
        request_id = identifier(request_id)
        ledger = self.project / ".bridge/operations" / (request_id + ".json")
        with locked(self.project / ".bridge", request_id):
            if ledger.exists():
                previous = read_json(ledger)
                if previous["status"] == "completed":
                    return previous["result"]
                raise BridgeError("operation_uncertain", "该请求已经提交或被中断；用保存的 Gear run_id 恢复，不重新启动优化")
            write_json(ledger, {"status": "submitted", "at": now(), "request_id": request_id})
            try:
                report = self.invoke("refine", timeout=timeout)
                if not isinstance(report.get("run_id"), str):
                    raise BridgeError("gear_invalid_response", "优化结果缺少 run_id")
            except Exception as exc:
                write_json(ledger, {"status": "needs_recovery", "request_id": request_id,
                                    "reason": type(exc).__name__, "action": "gear report / gear resume RUN_ID"})
                raise
            write_json(ledger, {"status": "completed", "request_id": request_id, "result": report})
            return report

    def export(self, run_id):
        return self.invoke("export", identifier(run_id))


def _checked(value, field="digest"):
    if gear_digest({k: v for k, v in value.items() if k != field}) != value.get(field):
        raise BridgeError("gear_identity_mismatch", f"Gear {field} 校验失败")


def target_resources(root, manifest):
    result = []
    expected = {item["path"]: item for item in manifest["resources"]}
    if {i["path"] for i in inventory(root)} != set(expected):
        raise BridgeError("target_files_changed", "导出配置的文件集合发生变化")
    for name, item in expected.items():
        path = relative_path(name)
        if path.parts[0] not in {"preset", "plugins", "prompts", "skills", "workflows"}:
            raise BridgeError("mutation_forbidden", name)
        actual = gear_digest(base64.b64encode((root / name).read_bytes()).decode("ascii"))
        if item["digest"] != actual or type(item.get("executable")) is not bool:
            raise BridgeError("target_files_changed", name)
        result.append(item)
    return result


def verify_gear_export(project, run_id):
    project, run_id = Path(project), identifier(run_id)
    run = project / ".gear/runs" / run_id
    exported = project / ".gear/exports" / run_id
    journal = read_json(run / "journal.json")
    _checked(journal)
    spec = journal["spec"]
    if gear_digest(spec) != journal["specDigest"]:
        raise BridgeError("gear_identity_mismatch", "运行规格发生变化")
    if journal.get("id") != run_id or journal.get("status") != "completed" or journal.get("gate", {}).get("status") != "accepted":
        raise BridgeError("not_adopted", "仅发布 Gear 已完成且通过采用检查的候选")
    if spec.get("validationMode") != "holdout" or not spec.get("heldOutTaskIds"):
        raise BridgeError("holdout_required", "共享题目研究结果不能发布为独立验证通过")
    candidate = next((c for c in journal["candidates"] if c["id"] == journal["nomineeId"]), None)
    if not candidate or candidate["id"] != journal["championId"]:
        raise BridgeError("candidate_mismatch", "提名与保留版本不一致")
    manifest = read_json(exported / "manifest.json")
    _checked(manifest)
    if manifest.get("version") != 2 or candidate["digest"] != manifest["digest"]:
        raise BridgeError("candidate_mismatch", "不支持的配置格式，或配置摘要不匹配")
    sealed = read_json(run / "target-manifests" / (candidate["id"] + ".json"))
    if sealed != manifest:
        raise BridgeError("candidate_mismatch", "导出配置与原评测配置不同")
    config = read_json(run / "project.json")
    if gear_digest(config) != spec["configDigest"] or config.get("target", {}).get("adapter") != "dsh":
        raise BridgeError("gear_identity_mismatch", "项目配置不是已评测的 DSH 项目")
    report = read_json(exported / "report.json")
    if (report.get("run_id"), report.get("retained"), report.get("nominee"), report.get("status")) != (run_id, candidate["id"], candidate["id"], "completed"):
        raise BridgeError("report_mismatch", "导出报告与运行记录不一致")
    if report.get("gate") != journal["gate"] or report.get("validation_mode") != "independent-held-out":
        raise BridgeError("report_mismatch", "导出报告缺少相同的独立采用结论")
    # Do not recalculate Gear's objective. Require complete exact cell bindings for the final comparison.
    reps = spec["budget"]["repetitions"]
    if type(reps) is not int or reps < 1:
        raise BridgeError("invalid_repetitions", "无效重复次数")
    for name, tasks, rows in [("seed", [t["id"] for t in spec["seedTasks"]], journal["seedEvidence"]),
                               ("held_out", spec["heldOutTaskIds"], journal["heldOutEvidence"])]:
        for cid in (spec["anchor"]["id"], candidate["id"]):
            selected = [r for r in rows if r["candidate_id"] == cid]
            required = {(t, n) for t in tasks for n in range(reps)}
            if len(selected) != len(required) or {(r["task_id"], r["repetition"]) for r in selected} != required:
                raise BridgeError("incomplete_evidence", f"{name}/{cid} 缺少完整对应的执行结果")
            if any(r.get("status") not in {"valid", "task_failed"} or type(r.get("score")) not in (int, float) for r in selected):
                raise BridgeError("invalid_evidence", name)
    target_resources(exported / "target", manifest)
    parent = read_json(run / "target-manifests" / (spec["anchor"]["id"] + ".json"))
    _checked(parent)
    before = {r["path"]: r for r in parent["resources"]}
    after = {r["path"]: r for r in manifest["resources"]}
    for name in set(before) | set(after):
        if before.get(name) != after.get(name):
            from .meta_contract import MUTABLE, PROTECTED
            parts = relative_path(name).parts
            if parts[0] not in MUTABLE or any(part in PROTECTED for part in parts):
                raise BridgeError('mutation_forbidden', '修改超出原生目标配置范围：' + name)
    return {"run_id": run_id, "candidate_id": candidate["id"], "config_ref": manifest["digest"],
            "runtime_identity": manifest["runtimeIdentity"], "benchmark_digest": spec["benchmarkDigest"],
            "manifest": manifest, "exported": str(exported.resolve()), "gate": journal["gate"]}


def publish(project, run_id, release_root, *, expected_previous=None):
    release_root = Path(release_root)
    with locked(release_root, "publication"):
        checked = verify_gear_export(project, run_id)
        active = release_root / "active.json"
        previous = read_json(active) if active.exists() else None
        if expected_previous is not None and (previous or {}).get("config_ref") != expected_previous:
            raise BridgeError("publication_conflict", "运行期间发布版本已经变化")
        version = checked["config_ref"][7:]
        directory = release_root / "versions" / version
        if not directory.exists():
            with staging(directory) as temp:
                shutil.copytree(Path(checked["exported"]) / "target", temp / "target")
                write_json(temp / "manifest.json", checked["manifest"])
                write_json(temp / "adoption.json", {k: v for k, v in checked.items() if k != "manifest"})
        target_resources(directory / "target", checked["manifest"])
        record = {"version": 1, "config_ref": checked["config_ref"], "directory": str(directory.resolve()),
                  "runtime_identity": checked["runtime_identity"], "run_id": run_id,
                  "previous": previous.get("config_ref") if previous else None, "published_at": now(),
                  "origin": "gear-accepted"}
        if previous and previous["config_ref"] == record["config_ref"]:
            return previous
        write_json(release_root / "history" / (run_id + ".json"), record)
        write_json(active, record)
        return record


def active_release(root, *, expected_runtime=None):
    active = read_json(Path(root) / "active.json")
    directory = Path(active["directory"])
    if directory.resolve().parent != (Path(root) / "versions").resolve():
        raise BridgeError("invalid_release_path", str(directory))
    manifest = read_json(directory / "manifest.json")
    _checked(manifest)
    if manifest["digest"] != active["config_ref"] or (expected_runtime is not None and active["runtime_identity"] != expected_runtime):
        raise BridgeError("runtime_mismatch", "发布配置与编码运行环境不匹配")
    target_resources(directory / "target", manifest)
    return active
