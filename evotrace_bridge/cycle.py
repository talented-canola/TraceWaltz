"""Explicit, resumable coordination; EvoTrace and Gear retain their own decisions."""
from __future__ import annotations
from pathlib import Path

from .common import BridgeError, digest, identifier, locked, now, read_json, write_json
from .workflow import evolve, model_access, produce


def run_cycle(config, plan, *, cycle_id, resume=False):
    if config.get('execution', {}).get('backend') != 'hitch':
        raise BridgeError('native_runtime_required', '完整循环使用原生容器评测配置')
    root = Path(config['data_root']) / 'cycles' / identifier(cycle_id)
    state_path = root / 'state.json'
    request_digest = digest(plan)
    with locked(root.parent, cycle_id):
        state = read_json(state_path) if state_path.exists() else {
            'plan_digest': request_digest, 'status': 'running', 'stages': {}, 'started_at': now()}
        if state['plan_digest'] != request_digest:
            raise BridgeError('cycle_plan_changed', '本轮已经绑定原计划，修改计划需要发起新一轮')
        if state['status'] == 'completed':
            return state
        write_json(root / 'plan.json', plan)

        def stage(name, action, recover=None):
            previous = state['stages'].get(name)
            if previous:
                if previous['status'] == 'completed':
                    return previous['result']
                if resume and recover is not None:
                    value = recover()
                    state['stages'][name] = {**previous, 'status': 'completed', 'result': value,
                        'finished_at': now(), 'recovery': 'verified completed native result; no resubmission'}
                    state['status'] = 'running'
                    write_json(state_path, state)
                    return value
                raise BridgeError('cycle_needs_recovery', '该步骤已提交但完成状态不明，先恢复原操作：' + name)
            state['stages'][name] = {'status': 'submitted', 'started_at': now()}
            write_json(state_path, state)
            try:
                value = action()
            except Exception as exc:
                state['status'] = 'needs_recovery'
                state['stages'][name].update(error={'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc)})
                write_json(state_path, state)
                raise
            state['stages'][name].update(status='completed', result=value, finished_at=now())
            write_json(state_path, state)
            return value

        produced = {}
        for task in plan.get('coding_tasks', []):
            name = identifier(task['task_id'])
            produced[name] = stage('coding-' + name, lambda task=task: produce(config, **task))

        for index, item in enumerate(plan.get('recorded_experience', [])):
            options = dict(item)
            source_task = options.pop('source_task', None)
            if source_task is not None:
                options['run_id'] = produced[source_task]['run_id']
            def compress(options=options):
                from .recorded_experience import compress_run
                with model_access(config):
                    return compress_run(config, **options)
            stage('recorded-experience-' + str(index + 1), compress)

        for index, operation in enumerate(plan.get('asset_operations', [])):
            if operation['operation'] not in {'validate', 'calibrate', 'harden', 'evolve_experience'}:
                raise BridgeError('unsupported_asset_operation', operation['operation'])
            def operate(operation=operation):
                from evotrace.integration import AssetService
                from evotrace.store import Store
                bundle = operation.get('bundle') or produced[operation['source_task']].get('bundle')
                if not bundle:
                    raise BridgeError('review_asset_unavailable', 'EvoTrace 审核未交付该步骤需要的任务包')
                held_out = operation.get('held_out')
                with model_access(config):
                    return AssetService(Store(Path(config['data_root']) / 'evotrace')).operate(
                        operation['operation'], bundle, held_out=held_out, **operation.get('options', {}))
            stage('asset-' + str(index + 1), operate)

        if plan.get('optimization'):
            options = plan['optimization']
            if options.get('assets'):
                from .native_export import export_native, freeze_native, verify_native
                assets = []
                for item in options['assets']:
                    path = item.get('path') or produced[item['source_task']].get('asset')
                    if not path:
                        raise BridgeError('review_asset_unavailable', '本轮所选任务未通过 EvoTrace 内部审核')
                    if item.get('source_task'):
                        # Experience/calibration operations may have added new
                        # products since production. Freeze the current complete
                        # asset here instead of reusing an earlier export.
                        from evotrace.integration import AssetService
                        from evotrace.store import Store
                        source = produced[item['source_task']]['bundle']
                        previous = verify_native(path)['request']
                        destination = root / 'assets' / identifier(item['source_task'])
                        def export(source=source, previous=previous, destination=destination, item=item):
                            products = AssetService(Store(Path(config['data_root']) / 'evotrace')).products(source)
                            return export_native(source, destination, family=previous['family'], split=previous['split'],
                                behavior=previous.get('behavior'), calibration_version=item.get('calibration_version'), products=products)
                        stage('export-' + identifier(item['source_task']), export)
                        path = destination
                    assets.append(Path(path))
                dataset = Path(config['data_root']) / 'datasets' / identifier(cycle_id)
                stage('freeze', lambda: freeze_native(assets, dataset, set_id=cycle_id))
            else:
                dataset = Path(options['dataset'])
            from .cycle_recovery import recover_gear_stage
            result = stage('gear', lambda: evolve(config, dataset=dataset,
                                                  request_id=options['request_id']),
                           recover=lambda: recover_gear_stage(config, options))
            if result['status'] != 'completed':
                state['status'] = 'needs_recovery'
                write_json(state_path, state)
                return state

        # Follow-up coding loads whatever Gear actually retained and published.
        # A rejected candidate leaves the prior publication intact.
        for task in plan.get('followup_tasks', []):
            stage('followup-' + identifier(task['task_id']), lambda task=task: produce(config, **task))
        state.update(status='completed', finished_at=now())
        write_json(state_path, state)
        return state
