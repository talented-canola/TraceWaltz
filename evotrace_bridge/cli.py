from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from .common import BridgeError, execute, identifier, inventory, now, read_json, write_json
from .exporter import export_bundle, freeze_set, verify_export
from .gear import GearClient, PINS, publish
from .recording import capture, ingest
from .runtime import bootstrap, rollback, run_task

PROJECT = Path(__file__).resolve().parents[1]


def initialize(root):
    import evotrace
    root = Path(root).resolve()
    source = Path(evotrace.__file__).resolve().parents[2]
    config_file = root / "bridge.json"
    if config_file.exists():
        raise BridgeError("already_initialized", str(config_file))
    root.mkdir(parents=True, exist_ok=True)
    node = str(PROJECT / '.runtime/node-v24.15.0-linux-x64/bin/node') if os.name != 'nt' else shutil.which('node') or 'node'
    dsh_root = PROJECT / ('.runtime/linux-dsh' if os.name != 'nt' else '.runtime/dsh')
    entry = source / "node_modules/@deepseek-ai/dsh/lib/bin.js"
    config = {"version": 1, "data_root": str(root), "evotrace_source": str(source),
              "coding_runtime": {"command": [node, str(dsh_root / 'node_modules/@deepseek-ai/dsh/lib/bin.js')],
                                 "version": PINS["dsh"], "model": "deepseek-flash", "timeout_seconds": 600},
              "review_runtime": {"command": [node, str(PROJECT / '.runtime/linux-review/node_modules/@deepseek-ai/dsh/lib/bin.js') if os.name != 'nt' else str(entry)],
                                 'module_root': str(PROJECT / '.runtime/linux-review'), "home": str(root / "review-home")},
              "gear": {"command": [node, str(PROJECT / '.runtime/gear-source/lib/cli.js')],
                       "project": str(root / "gear-project")},
              'optimization': {'rounds': 2, 'repetitions': 2, 'max_candidates': 4},
              'execution': {'backend': 'hitch', 'isolate_daily': True},
              'api': {'request_limit': 3000, 'ledger': 'shared-original', 'bind_host': '0.0.0.0',
                      'advertised_host': '172.17.0.1', 'port': 17980}, "pins": PINS}
    if (PROJECT / '.state/native-hitch-root').exists():
        config['execution']['hitch_root'] = (PROJECT / '.state/native-hitch-root').read_text().strip()
    if (PROJECT / '.state/hitch-executable').exists():
        config['execution']['hitch_executable'] = (PROJECT / '.state/hitch-executable').read_text().strip()
    shutil.copytree(PROJECT / "target", root / "target")
    bootstrap(root / "target", root / "releases", runtime_version=PINS["dsh"])
    gear_root = root / "gear-project"
    gear_root.mkdir()
    shutil.copytree(root / "target", gear_root / "target")
    (gear_root / "gear.yaml").write_text(
        "version: 1\nbenchmark: ./benchmark.yaml\ntarget: {adapter: dsh, path: ./target, profile: coding-target}\n"
        "meta: {profile: coding-meta}\nalgorithm: gepa\nobjective: {metric: pass_rate}\nvalidation: {mode: holdout}\n"
        "budget: {rounds: 1, max_evaluations: 40}\n", encoding="utf-8", newline="\n")
    write_json(config_file, config)
    return {"config": str(config_file), "status": "initialized", "data_root": str(root),
            'next': 'doctor, then produce / freeze / evolve'}


def doctor(config):
    commands = {name: shutil.which(name) for name in ['git', 'bwrap', 'rg', 'socat']}
    present = {name: all(Path(p).is_file() for p in config[key]['command'])
               for name, key in [('coding_runtime', 'coding_runtime'), ('gear_runtime', 'gear')]}
    missing = [k for k, value in {**commands, **present}.items() if not value]
    version = execute([*config['coding_runtime']['command'], '--version'], timeout=60) if present['coding_runtime'] else None
    return {"status": 'ready' if not missing and version and not version.returncode else 'needs_setup',
            "platform": sys.platform, "commands": commands, 'runtime_files': present,
            'coding_version': version.stdout.decode('utf-8', 'replace').strip() if version else None,
            "coding_version_required": config["coding_runtime"]["version"],
            "benchmark_imported": (Path(config["gear"]["project"]) / "benchmark.yaml").is_file(),
            "data_root": config["data_root"], "model_requests": 0,
            'execution': ('native Hitch/Harbor Docker; isolated Meta and daily workspace'
                          if config.get('execution', {}).get('backend') == 'hitch' else 'historical trusted-local Python'),
            'optional_container_path': {'docker': shutil.which('docker'), 'hitch': shutil.which('hitch')},
            "missing": missing}


def review(config, session_id, *, resume=False, resume_note=None):
    """Use the original reviewer runtime and /review handler; no new role prompts."""
    root, source = Path(config["data_root"]), Path(config["evotrace_source"])
    session_id = identifier(session_id)
    receipt = root / "reviews" / session_id / "command.json"
    final = receipt.with_name('review-result.json')
    from evotrace.provenance import source_revision
    session_dir = root / 'evotrace/sessions' / session_id
    revision = source_revision(session_dir, read_json(session_dir / 'trajectory.json'))
    request_path = receipt.with_name('request.json')
    if request_path.exists() and read_json(request_path)['source_revision'] != revision:
        raise BridgeError('review_source_changed', '审核已绑定另一版输入，请保留旧证据后发起新审核')
    if final.exists() and not resume:
        saved = read_json(final)
        previous_trace = Path(saved.get('source_session', ''))
        if previous_trace.is_file() and saved.get('source_session_bytes') == previous_trace.stat().st_size:
            return saved
    if resume and not receipt.exists():
        raise BridgeError('review_resume_missing', '恢复审核需要原来的会话与审核记录')
    if request_path.exists() and not receipt.exists() and read_json(request_path)['status'] != 'failed_before_model':
        raise BridgeError('review_needs_recovery', '审核已提交但未收到完成记录，先恢复原会话，不重复发起模型调用')
    patch = receipt.with_suffix(".patch.yml")
    patch.parent.mkdir(parents=True, exist_ok=True)
    home = Path(config['review_runtime']['home'])
    home.mkdir(parents=True, exist_ok=True)
    prepared = execute([config['review_runtime']['command'][0], PROJECT / 'tools/prepare-review.mjs', source, home], timeout=120)
    if prepared.returncode:
        raise BridgeError('review_prepare_failed', prepared.stderr.decode('utf-8', 'replace'))
    q = lambda s: json.dumps(str(s).replace("\\", "/"), ensure_ascii=False)
    patch.write_text("- id: headless-runner\n  disabled: true\n- id: ui-brand-evotrace\n  name: "
                     + q(source / 'harness/client-brand/lib/index.js') + "\n- id: agent-default-model\n  config:\n    provider: deepseek-official\n    model: "
                     + q(config['coding_runtime']['model']) + "\n- insert:\n    - id: bridge-review\n      name: "
                     + q(PROJECT / "harness/review-bridge.mjs") + "\n      config:\n        candidate: " + q(session_id)
                     + "\n        resume: " + ('true' if resume else 'false')
                     + "\n        resumeNote: " + q(resume_note or '')
                     + "\n        receipt: " + q(receipt) + "\n        moduleRoot: "
                     + q(config.get('review_runtime', {}).get('module_root', source)) + "\n", encoding="utf-8", newline="\n")
    env = {**os.environ, "EVOTRACE_HOME": str(root / "evotrace"), 'EVOTRACE_STORE_HOME': str(root / 'evotrace'),
           'EVOTRACE_CORE_ROOT': str(source), 'EVOTRACE_CORE_PYTHON': sys.executable,
           'EVOTRACE_PRESET_ROOT': str(home / '.agent-presets'), 'DSH_HOME': str(home),
           "DSH_TELEMETRY_DISABLED": "1", "PYTHONUTF8": "1"}
    if not Path(env["DSH_HOME"]).is_dir():
        raise BridgeError("review_home_missing", "独立审查 DSH 尚未配置模型连接")
    if not receipt.exists() or resume:
        if resume:
            archive = receipt.parent / 'attempts' / now().replace(':', '-')
            archive.mkdir(parents=True)
            for path in receipt.parent.iterdir():
                if path.is_file():
                    shutil.copyfile(path, archive / path.name)
        from .dsh import usage
        before_requests = usage()['reserved_requests']
        write_json(request_path, {'source_revision': revision, 'status': 'submitted', 'submitted_at': now(),
                                  'api_before': before_requests})
        process = execute([*config["review_runtime"]["command"], "--profile", "headless", "--patch",
                           source / "harness/evotrace.patch.yml", "--patch", patch, "--", "--", "review"],
                          cwd=root, env=env, timeout=1200)
        receipt.with_suffix(".stdout.txt").write_bytes(process.stdout)
        receipt.with_suffix(".stderr.txt").write_bytes(process.stderr)
        if process.returncode or not receipt.exists():
            after_requests = usage()['reserved_requests']
            write_json(request_path, {**read_json(request_path),
                       'status': 'failed_before_model' if after_requests == before_requests else 'needs_recovery',
                       'api_after': after_requests})
            raise BridgeError("review_failed", "审查未完成；已保存进程输出")
    command_result = read_json(receipt)
    from evotrace.integration import collect_review, read_session
    from evotrace.store import Store
    traces = []
    for path in home.rglob('session*.jsonl*'):
        if path.is_file() and any(event.get('type') == 'session' and event.get('id') == command_result['session_id']
                                  for event in read_session(path)[:1]):
            traces.append(path)
    if len(traces) != 1:
        raise BridgeError('review_trace_missing', '审核进程结束，但主会话记录未能唯一对应')
    result = collect_review(Store(root / 'evotrace'), candidate=session_id, token=command_result['review_token'],
                            session_path=traces[0], destination=receipt.parent)
    write_json(request_path, {**read_json(request_path), 'status': 'completed', 'outcome': result['status'],
                              'completed_at': now()})
    return result


def parser():
    p = argparse.ArgumentParser(description="TraceWaltz: DSH / EvoTrace / Gear integration")
    p.add_argument("--config", type=Path, default=PROJECT / ".state/bridge.json")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init").add_argument("--root", type=Path, default=PROJECT / ".state")
    sub.add_parser("doctor")
    cap = sub.add_parser("capture")
    cap.add_argument("repository", type=Path)
    cap.add_argument("destination", type=Path)
    imp = sub.add_parser("ingest")
    for name in ["trace", "before", "after"]:
        imp.add_argument("--" + name, type=Path, required=True)
    for name in ["task-id", "run-id", "instruction"]:
        imp.add_argument("--" + name, required=True)
    imp.add_argument("--purpose", choices=["daily", "evaluation"], default="daily")
    imp.add_argument("--verify", action="append", default=[])
    imp.add_argument("--exit-code", type=int, default=0)
    build = sub.add_parser("build")
    build.add_argument("session_id")
    rev = sub.add_parser("review")
    rev.add_argument("session_id")
    rev.add_argument('--resume', action='store_true')
    rev.add_argument('--resume-note')
    exp = sub.add_parser("export-task")
    exp.add_argument("bundle", type=Path)
    exp.add_argument("--task-id", required=True)
    exp.add_argument("--family", required=True)
    exp.add_argument("--split", choices=["seed", "held_out"], required=True)
    native = sub.add_parser('export-native', help='Export the original verifier, environment and full task as native Harbor')
    native.add_argument('bundle', type=Path)
    native.add_argument('--task-id', required=True)
    native.add_argument('--family', required=True)
    native.add_argument('--split', choices=['seed', 'held_out'], required=True)
    native.add_argument('--behavior', type=Path)
    native.add_argument('--calibration-version', type=Path)
    asset = sub.add_parser('asset', help='Run an existing operation inside EvoTrace')
    asset.add_argument('operation', choices=['validate', 'calibrate', 'harden', 'evolve_experience', 'products'])
    asset.add_argument('bundle', type=Path)
    asset.add_argument('--held-out', type=Path)
    asset.add_argument('--trials', type=int, default=5)
    asset.add_argument('--timeout', type=int, default=600)
    recorded = sub.add_parser('experience-run', help='Compress an actual coding recording inside EvoTrace')
    recorded.add_argument('run_id')
    recorded.add_argument('--request-id', required=True)
    recorded.add_argument('--timeout', type=int, default=600)
    frozen = sub.add_parser("freeze")
    frozen.add_argument("--set-id", required=True)
    frozen.add_argument("assets", nargs="+", type=Path)
    sub.add_parser("gear-import").add_argument("dataset", type=Path)
    sub.add_parser("gear-refine").add_argument("--request-id", required=True)
    sub.add_parser("publish").add_argument("run_id")
    sub.add_parser("rollback").add_argument("--reason", required=True)
    run = sub.add_parser("run")
    run.add_argument("repository", type=Path)
    for name in ["task-id", "run-id", "instruction"]:
        run.add_argument("--" + name, required=True)
    run.add_argument("--verify", action="append", default=[])
    prod = sub.add_parser('produce', help='Code, independently test, build EvoTrace task, export asset')
    prod.add_argument('repository', type=Path)
    for name in ['task-id', 'run-id', 'instruction', 'family']:
        prod.add_argument('--' + name, required=True)
    prod.add_argument('--verify', action='append', required=True)
    prod.add_argument('--split', choices=['seed', 'held_out'], required=True)
    prod.add_argument('--behavior', type=Path, help='Frozen task-specific behavior rubric; original verifier retained')
    opt = sub.add_parser('evolve', help='Run actual Gear GEPA, held-out gate and checked publication')
    opt.add_argument('--dataset', type=Path, required=True)
    opt.add_argument('--request-id', required=True)
    opt.add_argument('--resume', help='Resume this exact Gear run; unknown operations are not blindly replayed')
    sub.add_parser('status')
    cycle = sub.add_parser('cycle', help='Run an explicit complete integration plan with durable stage states')
    cycle.add_argument('plan', type=Path)
    cycle.add_argument('--cycle-id', required=True)
    cycle.add_argument('--resume', action='store_true', help='Reuse a verified completed Gear result; never resubmit uncertain work')
    demo = sub.add_parser("demo")
    demo.add_argument("--output", type=Path, default=PROJECT / "artifacts/demo")
    return p


def dispatch(args):
    if args.command == "init":
        return initialize(args.root)
    if args.command == "capture":
        return capture(args.repository, args.destination)
    if args.command == "demo":
        from .demo import run_demo
        return run_demo(args.output)
    config = read_json(args.config)
    root = Path(config["data_root"])
    client = GearClient(config["gear"]["project"], config["gear"]["command"])
    if args.command == "doctor":
        return doctor(config)
    if args.command == 'status':
        from .gateway import RequestLedger
        from .gear import active_release
        return {'active': active_release(root / 'releases'), 'api': RequestLedger(PROJECT / '.state/api').summary()}
    if args.command in {'produce', 'evolve'}:
        from .workflow import produce, evolve
        if args.command == 'produce':
            return produce(config, repository=args.repository, task_id=args.task_id, run_id=args.run_id,
                           instruction=args.instruction, verification_commands=args.verify, family=args.family, split=args.split,
                           behavior=read_json(args.behavior) if args.behavior else None)
        return evolve(config, dataset=args.dataset, request_id=args.request_id, resume=args.resume)
    if args.command == "ingest":
        return ingest(trace=args.trace, before=args.before, after=args.after, task_id=args.task_id, run_id=args.run_id,
                      instruction=args.instruction, store_root=root / "evotrace", purpose=args.purpose,
                      verification_commands=args.verify, exit_code=args.exit_code)
    if args.command == 'cycle':
        from .cycle import run_cycle
        return run_cycle(config, read_json(args.plan), cycle_id=args.cycle_id, resume=args.resume)
    if args.command == 'experience-run':
        from .recorded_experience import compress_run
        from .workflow import model_access
        with model_access(config):
            return compress_run(config, run_id=args.run_id, request_id=args.request_id, timeout=args.timeout)
    if args.command == "build":
        from evotrace.compiler import compile_session
        from evotrace.store import Store
        trajectory = read_json(root / "evotrace/sessions" / identifier(args.session_id) / "trajectory.json")
        if trajectory["outcome"]["status"] != "completed":
            raise BridgeError("incomplete_trajectory", "未完成的编码保留作诊断，不构建参考任务")
        bundle, manifest = compile_session(args.session_id, store=Store(root / "evotrace"))
        return {"bundle": str(bundle), "id": manifest["id"], "review": "not_run", "docker_validation": "not_run"}
    if args.command == "review":
        from .workflow import model_access
        with model_access(config):
            return review(config, args.session_id, resume=args.resume, resume_note=args.resume_note)
    if args.command == "export-task":
        return export_bundle(args.bundle, root / "assets" / identifier(args.task_id), family=args.family, split=args.split)
    if args.command == 'export-native':
        from .native_export import export_native
        from evotrace.integration import AssetService
        from evotrace.store import Store
        products = None
        if args.bundle.resolve().parent == (root / 'evotrace/benchmarks').resolve():
            products = AssetService(Store(root / 'evotrace')).products(args.bundle)
        return export_native(args.bundle, root / 'native-assets' / identifier(args.task_id), family=args.family,
                             split=args.split, behavior=read_json(args.behavior) if args.behavior else None,
                             calibration_version=args.calibration_version, products=products)
    if args.command == 'asset':
        from evotrace.integration import AssetService
        from evotrace.store import Store
        from .workflow import model_access
        service = AssetService(Store(root / 'evotrace'))
        if args.operation == 'products':
            return service.products(args.bundle)
        options = {'timeout': args.timeout}
        if args.operation != 'validate':
            options['trials'] = args.trials
            with model_access(config):
                return service.operate(args.operation, args.bundle, held_out=args.held_out, **options)
        return service.operate(args.operation, args.bundle, **options)
    if args.command == "freeze":
        if any((path / 'tracewaltz.asset.json').exists() for path in args.assets):
            from .native_export import freeze_native
            return freeze_native(args.assets, root / 'datasets' / identifier(args.set_id), set_id=args.set_id)
        return freeze_set(args.assets, root / "datasets" / identifier(args.set_id), set_id=args.set_id)
    if args.command == "gear-import":
        return client.import_set(args.dataset)
    if args.command == "gear-refine":
        from .workflow import model_access
        with model_access(config):
            return client.refine(args.request_id)
    if args.command == "publish":
        client.export(args.run_id)
        return publish(client.project, args.run_id, root / "releases")
    if args.command == "rollback":
        return rollback(root / "releases", reason=args.reason)
    if args.command == "run":
        from .workflow import model_access
        with model_access(config):
            return run_task(config, task_id=args.task_id, run_id=args.run_id, instruction=args.instruction,
                            repository=args.repository, verification_commands=args.verify)
    raise BridgeError("unknown_command", args.command)


def main():
    try:
        result = dispatch(parser().parse_args())
    except (BridgeError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "failed", "code": getattr(exc, "code", type(exc).__name__), "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0
