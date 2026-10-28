"""Recover a finished native Gear stage from its bound local completion evidence."""
from pathlib import Path

from .common import BridgeError, gear_digest, identifier, read_json
from .gear import active_release
from .native_export import verify_native_set


def recover_gear_stage(config, options):
    """Finish or resume the exact persisted Gear run; never create another one."""
    request_id = identifier(options['request_id'])
    job = Path(config['data_root']) / 'optimizations' / request_id
    if (job / 'result.json').exists():
        return completed_gear_stage(config, options)
    request = read_json(job / 'request.json')
    if request.get('request_id') != request_id or request.get('backend') != 'hitch':
        raise BridgeError('cycle_recovery_mismatch', '原优化请求与本轮计划不一致')
    dataset = Path(request['dataset'])
    if options.get('dataset') and Path(options['dataset']).resolve() != dataset.resolve():
        raise BridgeError('cycle_recovery_mismatch', '恢复计划指向不同题集')
    if verify_native_set(dataset)['digest'] != request['dataset_digest']:
        raise BridgeError('cycle_recovery_mismatch', '冻结题集已变化')
    journals = list((job / 'gear-project/.gear/runs').glob('*/journal.json'))
    if len(journals) != 1:
        raise BridgeError('cycle_needs_recovery', '无法唯一确定原生 Gear 运行，不重新提交优化')
    journal = read_json(journals[0])
    run_id = identifier(journal['id'])
    if journals[0].parent.name != run_id or gear_digest({k: v for k, v in journal.items() if k != 'digest'}) != journal.get('digest'):
        raise BridgeError('cycle_recovery_mismatch', '原生 Gear 运行记录不一致')
    if journal['status'] not in {'needs_recovery', 'ready', 'completed'}:
        raise BridgeError('cycle_needs_recovery', '原生 Gear 尚未完成或仍在执行，先检查原工作进程')
    from .workflow import evolve
    result = evolve(config, dataset=dataset, request_id=request_id, resume=run_id)
    if result['status'] != 'completed':
        raise BridgeError('cycle_needs_recovery', '原生 Gear 尚未完成，保留同一运行继续检查')
    return completed_gear_stage(config, options)


def completed_gear_stage(config, options):
    request_id = identifier(options['request_id'])
    job = Path(config['data_root']) / 'optimizations' / request_id
    result_path = job / 'result.json'
    if not result_path.exists():
        raise BridgeError('cycle_needs_recovery', 'Gear 尚无完整回执，先查询或恢复原 Gear 运行')
    request, result = read_json(job / 'request.json'), read_json(result_path)
    if (request.get('request_id') != request_id or result.get('request_id') != request_id
            or result.get('status') != 'completed' or Path(result['directory']).resolve() != job.resolve()):
        raise BridgeError('cycle_recovery_mismatch', 'Gear 完成回执与本轮计划不一致')
    dataset = Path(request['dataset'])
    if options.get('dataset') and Path(options['dataset']).resolve() != dataset.resolve():
        raise BridgeError('cycle_recovery_mismatch', '恢复计划指向不同题集')
    if verify_native_set(dataset)['digest'] != request['dataset_digest']:
        raise BridgeError('cycle_recovery_mismatch', '冻结题集已变化')
    run_id = identifier(result['run_id'])
    journal = read_json(job / 'gear-project/.gear/runs' / run_id / 'journal.json')
    if (journal.get('id') != run_id or journal.get('status') != 'completed'
            or gear_digest({key: value for key, value in journal.items() if key != 'digest'}) != journal.get('digest')
            or journal.get('gate') != result.get('gate')):
        raise BridgeError('cycle_recovery_mismatch', '原生 Gear 尚未完成或结果不一致')
    if (journal.get('gate') or {}).get('status') == 'accepted':
        publication = result.get('published')
        actual = active_release(Path(config['data_root']) / 'releases')
        if (not publication or publication.get('run_id') != run_id or publication != actual
                or actual.get('origin') != 'gear-accepted'):
            raise BridgeError('cycle_recovery_mismatch', '已采用配置尚未发布，或发布指向已变化')
    elif result.get('published') is not None:
        raise BridgeError('cycle_recovery_mismatch', '未采用的 Gear 运行出现了发布记录')
    return result
