"""Recover actual completed Meta work without changing the frozen provider.

Only an unambiguous task-field alias is normalized. The original trace, model
file, journal, scores and candidate files remain evidence, not editable inputs.
"""
import argparse
import copy
import json
import shutil
import sys
from pathlib import Path

import psutil

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from evotrace_bridge.common import (
    BridgeError, digest, execute, file_digest, gear_digest, identifier, inventory,
    locked, now, read_json, staging, write_json,
)
from evotrace_bridge.gear import target_resources
from evotrace_bridge.meta_contract import candidate_changes, meta_result
from evotrace_bridge.process_provider import handle, implementation, response
from evotrace.integration import read_session


def normalize_groups(raw, allowed):
    value = copy.deepcopy(raw)
    if not isinstance(value, dict) or not isinstance(value.get('clusters', []), list):
        raise BridgeError('recovery_schema', '结果或任务分组结构无效')
    mappings = []
    for row in value.get('clusters', []):
        if not isinstance(row, dict):
            raise BridgeError('recovery_schema', '任务分组必须是对象')
        if 'affected_tasks' in row:
            if 'task_ids' in row and row['task_ids'] != row['affected_tasks']:
                raise BridgeError('recovery_ambiguous', '两个任务字段含义冲突，不自动修正')
            if 'task_ids' not in row:
                row['task_ids'] = copy.deepcopy(row['affected_tasks'])
                mappings.append('clusters[].affected_tasks -> task_ids')
        tasks = row.get('task_ids')
        if not isinstance(tasks, list) or any(not isinstance(t, str) or t not in allowed for t in tasks):
            raise BridgeError('recovery_schema', '分组任务无效或不在本操作授权范围')
    if not mappings:
        raise BridgeError('recovery_not_applicable', '不存在本恢复工具处理的字段名差异')
    return value, mappings


def check_candidate(candidate, parent, operation):
    changes = candidate_changes(inventory(parent), candidate)
    if operation == 'diagnose_seed' and changes:
        raise BridgeError('recovery_mutation', '只读诊断修改了候选')
    if operation == 'generate_candidate' and not changes:
        raise BridgeError('recovery_empty_candidate', '没有实际配置修改')
    return changes


def recover(job, *, kind='generate_candidate', apply=False):
    job = Path(job).resolve()
    journals = list((job / 'gear-project/.gear/runs').glob('*/journal.json'))
    if len(journals) != 1:
        raise BridgeError('recovery_scope', '需要唯一明确的原轮运行')
    journal_path = journals[0]
    journal = read_json(journal_path)
    journal_file_digest = file_digest(journal_path)
    if (journal['status'] != 'needs_recovery' or
            gear_digest({k: v for k, v in journal.items() if k != 'digest'}) != journal['digest']):
        raise BridgeError('recovery_scope', '原轮不是完整可信的待恢复状态')
    operations = [v for v in journal['operations']
                  if v['request']['operation']['kind'] == kind and v['status'] == 'submitted']
    if len(operations) != 1:
        raise BridgeError('recovery_scope', '需要唯一已提交且未收齐回执的原操作')
    request = operations[0]['request']
    configuration = read_json(job / 'profiles/coding-meta.json')['configuration']
    directory = Path(configuration['state_root']) / request['runId'] / digest(request['operationId'])[7:]
    payload = read_json(directory / 'request.json')
    if (payload['request'] != request or payload['configuration'] != configuration
            or payload['implementationIdentity'] != implementation(configuration) or request['partition'] != 'seed'):
        raise BridgeError('recovery_binding', '请求、配置、原实现或任务划分不一致')
    marker = read_json(directory / 'started.json')
    if marker['request_digest'] != digest(request) or (directory / 'cancelled.json').exists():
        raise BridgeError('recovery_binding', '来源不一致或已取消')
    try:
        proc = psutil.Process(marker['pid'])
        if proc.is_running() and abs(proc.create_time() - marker['create_time']) < .01:
            raise BridgeError('recovery_busy', '原工作进程仍在执行')
    except psutil.NoSuchProcess:
        pass
    if (directory / 'receipt.json').exists():
        inspected = handle({**payload, 'action': 'inspect'})
        if inspected.get('state') != 'complete':
            raise BridgeError('recovery_inspection', '已有回执未被原查询接口接收')
        return {'status': 'already_recovered', 'new_model_requests': 0, 'native_inspect_complete': True}

    execution = read_json(directory / 'execution.json')
    if execution['status'] != 'completed' or execution['exit_code'] != 0 or execution['tool_calls'] < 1:
        raise BridgeError('recovery_execution', '没有已完成的真实工具执行')
    trace = Path(execution['trace']).resolve()
    if not trace.is_relative_to((directory / 'sandbox/output').resolve()):
        raise BridgeError('recovery_trace', '会话不属于此操作')
    sessions = [e for e in read_session(trace) if e.get('type') == 'session']
    if len(sessions) != 1 or sessions[0]['id'] != execution['session_id']:
        raise BridgeError('recovery_trace', '执行记录和原始会话不一致')
    parent_id = identifier(request['operation'].get('parent_id') or request['operation']['candidate_id'])
    targets = Path(payload['context']['targets'])
    parent = targets / parent_id
    manifest = read_json(journal_path.parent / 'target-manifests' / (parent_id + '.json'))
    target_resources(parent, manifest)
    candidate = directory / 'sandbox/candidate'
    changed = check_candidate(candidate, parent, kind)
    candidate_inventory, parent_inventory = inventory(candidate), inventory(parent)
    raw_path = directory / 'sandbox/output/meta-result.json'
    normalized, mappings = normalize_groups(read_json(raw_path), set(request['operation']['task_ids']))
    output = directory / 'postprocessing-recovery'
    output.mkdir(exist_ok=True)
    write_json(output / 'normalized-result.json', normalized)
    data = meta_result(output / 'normalized-result.json', task_ids=set(request['operation']['task_ids']))
    if data['declined']:
        raise BridgeError('recovery_declined', '本工具不将已拒绝提案恢复成有效候选')
    if kind == 'generate_candidate':
        checked = execute([configuration['dsh_command'][0], str(PROJECT / 'tools/check-target.mjs'), str(candidate)], timeout=60)
        (output / 'target-check.stdout.txt').write_bytes(checked.stdout)
        (output / 'target-check.stderr.txt').write_bytes(checked.stderr)
        if checked.returncode:
            raise BridgeError('recovery_loading', '候选未通过原生配置及语法检查')

    observation = {'at': now(), 'kind': 'completed-' + kind + '-field-normalization',
        'request_digest': digest(request), 'provider_implementation': payload['implementationIdentity'],
        'raw_result_digest': file_digest(raw_path), 'trace_digest': file_digest(trace),
        'normalized_result_digest': file_digest(output / 'normalized-result.json'),
        'candidate_digest': digest(candidate_inventory), 'parent_digest': digest(parent_inventory),
        'changed_files': changed, 'mapping': mappings, 'source_model_work_replayed': False,
        'new_model_requests': 0, 'original_result_unchanged': True}
    write_json(output / 'validation.json', observation)
    cid = identifier(request['operation']['candidate_id'])
    receipt_candidate = {'id': cid, 'parent_id': parent_id, 'digest': digest(candidate_inventory)} if kind == 'generate_candidate' else None
    receipt = response(request, candidate=receipt_candidate, data={**data, 'changed_files': changed},
        calls=execution['model_requests'], input_tokens=execution['input_tokens'], output_tokens=execution['output_tokens'],
        provenance={'kind': 'real-dsh-meta', 'partition': 'seed', 'execution': execution,
            'changed_files': changed, 'evidence': 'complete-admitted-seed-input', 'postprocessing_recovery': observation})
    if apply:
        with locked(configuration['state_root'], request['operationId']):
            if (directory / 'receipt.json').exists() or (directory / 'cancelled.json').exists():
                raise BridgeError('recovery_changed', '回执或取消状态已变化，先重新查询')
            if (file_digest(journal_path) != journal_file_digest
                    or file_digest(raw_path) != observation['raw_result_digest']
                    or file_digest(trace) != observation['trace_digest']
                    or implementation(configuration) != payload['implementationIdentity']
                    or inventory(candidate) != candidate_inventory or inventory(parent) != parent_inventory):
                raise BridgeError('recovery_changed', '验证后原运行、实现或产物发生变化')
            if receipt_candidate:
                destination = targets / cid
                prepared = output / 'prepared-candidate.json'
                expected = {'request_digest': digest(request), 'candidate': receipt_candidate}
                if destination.exists():
                    if not prepared.exists() or read_json(prepared) != expected or inventory(destination) != candidate_inventory:
                        raise BridgeError('recovery_candidate_exists', '已有候选来源或内容不同，不覆盖')
                else:
                    write_json(prepared, expected)
                    with staging(destination) as temporary:
                        shutil.copytree(candidate, temporary, dirs_exist_ok=True)
                        if inventory(temporary) != candidate_inventory:
                            raise BridgeError('recovery_copy', '复制后的实际候选与原执行不一致')
            write_json(directory / 'meta-result.json', data)
            write_json(directory / 'receipt.json', {'request_digest': digest(request), 'receipt': receipt})
        inspected = handle({**payload, 'action': 'inspect'})
        if inspected != {'state': 'complete', 'receipt': receipt}:
            raise BridgeError('recovery_inspection', '原生查询未返回一致回执')
        write_json(output / 'inspection.json', inspected)
    return {'status': 'recovered' if apply else 'validated', 'kind': kind, 'changed_files': changed,
            'new_model_requests': 0, 'original_result_preserved': True, 'native_inspect_complete': apply}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('job', type=Path)
    parser.add_argument('--kind', choices=['diagnose_seed', 'generate_candidate'], default='generate_candidate')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(recover(args.job, kind=args.kind, apply=args.apply), ensure_ascii=False))
