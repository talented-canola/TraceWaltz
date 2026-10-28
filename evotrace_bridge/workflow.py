"""Public coding -> task production -> real Gear comparison -> release workflow."""
from __future__ import annotations

import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

from .common import BridgeError, digest, identifier, locked, now, read_json, staging, write_json
from .exporter import export_bundle, verify_set
from .gateway import gateway
from .gear import GearClient, active_release, publish
from .process_dataset import export_process_dataset
from .runtime import run_task

PROJECT = Path(__file__).resolve().parents[1]


@contextmanager
def model_access(config):
    """All public commands share the original ledger and its explicit authorization."""
    before = os.environ.copy()
    with locked(PROJECT / '.state/api', 'paid-execution'):
        try:
            node = Path(config['coding_runtime']['command'][0]).parent
            os.environ['PATH'] = str(node) + os.pathsep + str(Path(sys.executable).parent) + os.pathsep + before.get('PATH', '')
            os.environ.update(PYTHONUTF8='1', GEAR_PYTHON=sys.executable,
                              GEAR_HOME=str(PROJECT / '.state/gear-home'),
                              EVOTRACE_DSH_EXECUTABLE=str(PROJECT / 'tools/review-dsh'),
                              EVOTRACE_CORE_PYTHON=sys.executable)
            if config.get('review_runtime', {}).get('home'):
                os.environ['DSH_HOME'] = config['review_runtime']['home']
            api = config.get('api', {})
            with gateway(PROJECT / '.state/api', limit=api.get('request_limit'),
                         bind_host=api.get('bind_host', '127.0.0.1'), advertised_host=api.get('advertised_host'),
                         port=api.get('port', 0)) as service:
                os.environ.update(DEEPSEEK_API_KEY=service['token'], DEEPSEEK_BASE_URL=service['url'])
                if config.get('execution', {}).get('backend') == 'hitch':
                    from .container_runtime import ensure_hitch
                    ensure_hitch(config)
                yield service
        finally:
            os.environ.clear()
            os.environ.update(before)


def produce(config, *, repository, task_id, run_id, instruction, verification_commands, family, split, behavior=None):
    if not verification_commands:
        raise BridgeError('verification_required', '生产评测题需要至少一条实际测试命令')
    task_id, run_id = identifier(task_id), identifier(run_id)
    with model_access(config):
        record = run_task(config, repository=repository, task_id=task_id, run_id=run_id,
                          instruction=instruction, verification_commands=verification_commands)
        if config.get('execution', {}).get('backend') == 'hitch':
            # EvoTrace owns role sequencing, route selection, construction and
            # validation. The outer coordinator consumes its published result.
            from .cli import review
            from .native_export import export_native, verify_native
            root = Path(config['data_root'])
            receipt_path = root / 'runs' / run_id / 'production.json'
            if receipt_path.exists():
                previous = read_json(receipt_path)
                if previous['session_id'] != record['ingestion']['session_id']:
                    raise BridgeError('production_conflict', '任务产物对应了另一份编码记录')
                if previous.get('asset') and verify_native(previous['asset'])['digest'] != previous['asset_digest']:
                    raise BridgeError('production_conflict', '正式任务导出发生变化')
                latest_review = root / 'reviews' / record['ingestion']['session_id'] / 'review-result.json'
                if previous.get('asset') or not latest_review.exists() or read_json(latest_review)['status'] == previous['status'].removeprefix('review_'):
                    return previous
                write_json(receipt_path.with_name('production-before-review-recovery.json'), previous)
            checked = review(config, record['ingestion']['session_id'])
            receipt = {'status': 'review_' + checked['status'], 'run_id': run_id,
                       'session_id': record['ingestion']['session_id'], 'config_ref': record['config_ref'],
                       'review': str(root / 'reviews' / record['ingestion']['session_id'] / 'review-result.json'),
                       'coding_status': record['status'], 'bundle': checked.get('bundle'), 'completed_at': now()}
            if checked['status'] == 'ready':
                from evotrace.integration import AssetService
                from evotrace.store import Store
                asset = root / 'native-assets' / task_id
                products = AssetService(Store(root / 'evotrace')).products(checked['bundle'])
                exported = export_native(checked['bundle'], asset, family=family, split=split, behavior=behavior, products=products)
                receipt.update(status='produced', asset=str(asset), asset_digest=exported['digest'],
                               verification='evotrace-review-and-native-verifier')
            write_json(receipt_path, receipt)
            return receipt
    if record['status'] != 'completed':
        raise BridgeError('coding_task_failed', '代码未通过验证，日志已保留，不生产参考任务')
    root = Path(config['data_root'])
    receipt_path = root / 'runs' / run_id / 'production.json'
    if receipt_path.exists():
        previous = read_json(receipt_path)
        if (previous['session_id'], previous['config_ref']) != (record['ingestion']['session_id'], record['config_ref']):
            raise BridgeError('production_conflict', '已有任务包与实际编码记录不一致')
        # EvoTrace intentionally refuses rebuilding an existing bundle. Reuse and
        # verify the original export rather than invoking the compiler again.
        checked = export_bundle(previous['bundle'], root / 'assets' / task_id, family=family, split=split)
        if checked['digest'] != previous['asset_digest']:
            raise BridgeError('production_conflict', '已有任务导出内容发生变化')
        return previous
    from evotrace.compiler import compile_session
    from evotrace.store import Store
    bundle, manifest = compile_session(record['ingestion']['session_id'], store=Store(root / 'evotrace'))
    asset = root / 'assets' / task_id
    exported = export_bundle(bundle, asset, family=family, split=split)
    receipt = {'status': 'produced', 'run_id': run_id, 'session_id': record['ingestion']['session_id'],
               'config_ref': record['config_ref'], 'bundle': str(bundle), 'asset': str(asset),
               'asset_digest': exported['digest'], 'execution': str(root / 'runs' / run_id / 'execution.json'),
               'review': 'optional_not_run', 'verification': 'actual-local-tests', 'completed_at': now()}
    write_json(root / 'runs' / run_id / 'production.json', receipt)
    return receipt


def gear_configuration(config):
    options = config.get('optimization', {})
    metric = {'id': 'model_requests', 'revision': '1', 'unit': 'requests', 'direction': 'minimize',
              'source': {'path': 'originalResult.model_requests', 'extractor': 'number-v1'},
              'range': {'min': 0, 'max': config.get('api', {}).get('request_limit', 3000)}, 'granularity': 'trial', 'repetitionReducer': 'mean',
              'taskReducer': 'weighted-mean', 'comparisonPrecision': 1e-9,
              'measurement': {'kind': 'actual', 'scope': 'all DSH API requests including title and retries',
                'providerModels': ['deepseek-flash'], 'priceSnapshot': 'request-count-only-no-price-v1',
                'tokenAccounting': 'gateway prompt plus completion usage, cached input counted once',
                'timeBoundary': 'before DSH launch through process exit', 'retryAccounting': 'every reserved outgoing POST'}}
    return {'version': 1, 'benchmark': './benchmark.yaml',
            'target': {'adapter': 'dsh', 'path': './target', 'profile': 'coding-target'},
            'meta': {'profile': 'coding-meta'}, 'algorithm': 'gepa', 'metrics': [metric],
            'objective': {'terms': [{'metric': 'pass_rate', 'weight': 1},
                                   {'metric': 'model_requests', 'weight': -options.get('request_weight', 0.01)}],
                          'constraints': [{'metric': 'pass_rate', 'rule': 'no_regression', 'reference': 'initial_baseline'}]},
            'validation': {'mode': 'holdout'},
            'budget': {'rounds': options.get('rounds', 1), 'max_evaluations': 40, 'max_candidates': 2,
                       'repetitions': options.get('repetitions', 2)},
            'evaluation': {'repair': {'max_retries': 0, 'max_projections': 0, 'backoff_ms': [1]}}}


def prepare_optimization(config, dataset, request_id):
    if os.name == 'nt':
        raise BridgeError('linux_required', 'Gear 需要 Linux；Windows 使用 evotrace-loop.cmd')
    if config.get('execution', {}).get('backend') == 'hitch':
        from .native_optimization import prepare_native_optimization
        return prepare_native_optimization(config, dataset, request_id)
    root = Path(config['data_root']) / 'optimizations' / identifier(request_id)
    frozen = verify_set(dataset)
    if (root / 'request.json').exists():
        saved = read_json(root / 'request.json')
        if saved['dataset_digest'] != frozen['digest']:
            raise BridgeError('operation_conflict', '优化编号已经绑定另一批任务')
        return root, saved
    blocked = set(config.get('blocked_source_digests', []))
    retired = [task['task_id'] for task in frozen['tasks'] if task['source_digest'] in blocked]
    if retired:
        raise BridgeError('retired_task', '任务集含有已退役样本，请使用新版任务集：' + ', '.join(retired))
    selected = active_release(Path(config['data_root']) / 'releases')
    request = {'version': 1, 'request_id': request_id, 'dataset': str(Path(dataset).resolve()),
               'dataset_digest': frozen['digest'], 'baseline_ref': selected['config_ref'],
               'configuration': gear_configuration(config)}
    with staging(root) as temp:
        export_process_dataset(dataset, temp / 'gear-project')
        shutil.copytree(Path(selected['directory']) / 'target', temp / 'gear-project/target')
        write_json(temp / 'gear-project/gear.yaml', request['configuration'])
        provider = {'command': [str(PROJECT / 'tools/provider.sh')], 'workingDirectory': str(PROJECT),
                    'passEnv': ['PATH', 'TEMP', 'TMP', 'PYTHONUTF8', 'DEEPSEEK_BASE_URL', 'DEEPSEEK_API_KEY'],
                    'configuration': {'state_root': str(root / 'provider'),
                        'dsh_version': config['coding_runtime']['version'],
                        'dsh_command': config['coding_runtime']['command'],
                        'task_timeout': config['coding_runtime'].get('timeout_seconds', 600)},
                    'timeoutMs': 1200000}
        for role in ('coding-target', 'coding-meta'):
            write_json(temp / 'profiles' / (role + '.json'), provider)
        write_json(temp / 'request.json', request)
    return root, request


def evolve(config, *, dataset, request_id, resume=None):
    """Real Gear owns GEPA, candidate snapshots, evidence, final gate and export."""
    root, request = prepare_optimization(config, dataset, request_id)
    with locked(Path(config['data_root']) / 'optimizations', identifier(request_id)):
        if (root / 'result.json').exists() and resume is None:
            return read_json(root / 'result.json')
        client = GearClient(root / 'gear-project', config['gear']['command'])
        with model_access(config) as service:
            os.environ['GEAR_PROFILE_ROOT'] = str(root / 'profiles')
            # These short-lived IPC objects require Unix sockets; DrvFS has none.
            ipc = Path('/tmp/evotrace-gear-ipc')
            ipc.mkdir(exist_ok=True)
            os.environ['TMPDIR'] = str(ipc)
            if request.get('backend') == 'hitch' and not (root / 'import.json').exists():
                write_json(root / 'import.json', client.import_set(dataset))
            write_json(root / 'benchmark-check.json', client.invoke('benchmark', 'check'))
            write_json(root / 'plan.json', client.invoke('refine', '--dry-run'))
            starting = service['ledger'].summary()
            report = client.invoke('resume', identifier(resume), timeout=None) if resume else client.refine(request_id)
            write_json(root / 'report.json', report)
            publication = None
            if report.get('status') == 'completed':
                write_json(root / 'export.json', client.export(report['run_id']))
                if report.get('gate', {}).get('status') == 'accepted':
                    publication = publish(client.project, report['run_id'], Path(config['data_root']) / 'releases',
                                          expected_previous=request['baseline_ref'])
                    write_json(root / 'publication.json', publication)
            result = {'status': report.get('status'), 'request_id': request_id, 'run_id': report['run_id'],
                      'gate': report.get('gate'), 'published': publication, 'directory': str(root),
                      'api': service['ledger'].summary(), 'api_before': starting, 'finished_at': now()}
            write_json(root / 'result.json', result)
            return result
