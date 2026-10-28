"""Adapt one actual coding execution to EvoTrace's recording experience operation."""
from pathlib import Path

from .common import BridgeError, identifier, read_json


def compress_run(config, *, run_id, request_id, timeout=600):
    from evotrace.calibration import DeepSeekHarnessSolver
    from evotrace.integration import AssetService
    from evotrace.store import Store
    root = Path(config['data_root'])
    record = read_json(root / 'runs' / identifier(run_id) / 'execution.json')
    if record.get('status') not in {'completed', 'task_failed'} or not record.get('ingestion') or not record.get('dsh'):
        raise BridgeError('recording_incomplete', '编码执行尚未结束或缺少原始会话，不启动经验压缩')
    store = Store(root / 'evotrace')
    dsh = record['dsh']
    source = {'session_id': record['ingestion']['session_id'], 'run_id': run_id,
              'task_id': record['request']['task_id'], 'execution_status': record['status'],
              'input_digest': record['ingestion']['input_digest'], 'config_ref': record['config_ref'],
              'before_snapshot': record['before_snapshot'], 'after_snapshot': record['after_snapshot']}
    # Initialization does not call the model. EvoTrace owns compression and
    # the durable replay checks; the bridge passes the original trace pointer.
    return AssetService(store).compress_recording(runner=DeepSeekHarnessSolver(store),
        solver_result={**dsh, 'session_log_path': dsh['trace']}, workspace=record['workspace'],
        source=source, request_id=identifier(request_id), timeout=timeout)
