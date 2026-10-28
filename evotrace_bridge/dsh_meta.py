"""Full executable DSH Meta behind Gear's public operation protocol."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from .common import BridgeError, digest, execute, identifier, inventory, read_json, relative_path, write_json
from .dsh import solve
from .meta_contract import candidate_changes, meta_result, result_contract, result_example

PROJECT = Path(__file__).resolve().parents[1]


def sandbox_command(command, *, candidate, materials, output, mutable, extra_read=()):
    if os.name == 'nt':
        raise BridgeError('linux_required', '完整改进执行器在 Linux 隔离环境运行')
    bwrap = shutil.which('bwrap')
    if not bwrap:
        raise BridgeError('isolation_missing', '完整改进 Agent 需要既定的文件访问隔离')
    args = [bwrap, '--die-with-parent', '--new-session', '--unshare-user', '--uid', '0', '--gid', '0',
            '--unshare-pid', '--unshare-ipc', '--unshare-uts', '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp']
    for system in ('/usr', '/lib', '/lib64', '/bin', '/sbin'):
        if Path(system).exists():
            args += ['--ro-bind', system, system]
    for system in ('/etc/ssl', '/etc/resolv.conf', '/etc/hosts', '/etc/nsswitch.conf', '/etc/passwd', '/etc/group'):
        if Path(system).exists():
            args += ['--ro-bind', system, system]
    runtimes = {PROJECT / '.runtime/node-v24.15.0-linux-x64', PROJECT / '.runtime/linux-dsh', PROJECT / 'harness/meta',
                PROJECT / 'harness/input-file-startup.mjs',
                PROJECT / '.runtime/linux-python', PROJECT / '.runtime/linux-tools', *map(Path, extra_read)}
    for path in sorted(runtimes, key=str):
        args += ['--ro-bind', str(path), str(path)]
    args += ['--ro-bind', str(materials), str(materials),
             '--bind' if mutable else '--ro-bind', str(candidate), str(candidate),
             '--bind', str(output), str(output), '--chdir', str(candidate), '--', *command]
    return args


def propose(request, context, configuration, directory):
    from .process_provider import response
    if request.get('partition') != 'seed':
        raise BridgeError('heldout_meta_forbidden', '最终留出材料不能进入改进端')
    operation = request['operation']
    kind = operation['kind']
    if kind not in {'diagnose_seed', 'generate_candidate'}:
        raise BridgeError('unsupported_meta_operation', kind)
    parent_id = identifier(operation.get('parent_id') or operation['candidate_id'])
    parent = Path(context['targets']) / parent_id
    root = directory / 'sandbox'
    candidate, materials, output = root / 'candidate', root / 'input', root / 'output'
    for path in (materials, output):
        path.mkdir(parents=True)
    shutil.copytree(parent, candidate)
    for path in candidate.rglob('*'):
        path.chmod(path.stat().st_mode | (0o700 if path.is_dir() else 0o600))
    before = inventory(candidate)
    task_ids = set(operation['task_ids'])
    tasks = [t for t in context['tasks'] if t['id'] in task_ids]
    if {t['id'] for t in tasks} != task_ids:
        raise BridgeError('meta_task_missing', '改进操作包含未授权或缺失题目')
    evidence = [row for row in request.get('evidence', []) if row['task_id'] in task_ids]
    if any(row.get('scope') == 'held_out' for row in evidence):
        raise BridgeError('heldout_meta_forbidden', '最终留出反馈不能用于改进')
    inputs = {'operation': operation, 'tasks': tasks, 'evidence': evidence,
              'mutation_roots': ['preset', 'plugins', 'prompts', 'skills', 'workflows'],
              'experience': request.get('experience'),
              'result_schema': result_contract(task_ids), 'result_example': result_example(task_ids)}
    if configuration.get('require_complete_evidence'):
        from .full_evidence import materialize
        inputs['complete_observations'] = materialize(request, context, configuration, materials / 'observations')
    # No event-count or text-length truncation: these are the actual complete admitted seed cells.
    write_json(materials / 'operation.json', inputs)
    write_json(materials / 'result-schema.json', inputs['result_schema'])
    write_json(materials / 'result-example.json', inputs['result_example'])
    public_root = configuration.get('public_materials')
    if public_root:
        index = read_json(Path(public_root) / 'index.json')
        for name in task_ids:
            entry = index['tasks'].get(name)
            if not entry or entry.get('split') != 'seed':
                raise BridgeError('meta_material_missing', '优化任务的公开材料缺失')
            source = Path(public_root) / relative_path(entry['directory'])
            inventory(source)
            shutil.copytree(source, materials / 'tasks' / identifier(name))
    result_path = output / 'meta-result.json'
    instruction = ('You are executing a Gear ' + kind + ' operation. Read the complete task data in '
                   + str(materials / 'operation.json') + '. Additional public task and evidence files, if supplied, are in '
                   + str(materials) + '. The candidate configuration is your current directory. '
                   + ('Inspect and diagnose without editing it. ' if kind == 'diagnose_seed' else
                      'Use tools to investigate and implement the justified hypothesis by editing the candidate. ')
                   + 'Check relevant syntax and loading entrypoints. Do not launch a formal benchmark; Gear owns evaluation. '
                   + 'Write the requested result JSON to ' + str(result_path) + ' after actual investigation. '
                   + 'The complete result schema and an example are supplied in operation.json and result-schema.json/result-example.json. '
                   + 'Use task_ids for groups, drawn only from the supplied task IDs; clusters may be []. '
                   + 'Use this supplied protocol directly; previous receipts or internal protocol source are not required. '
                   + 'Preserve complete evidence. Decline if no justified change exists.')
    instruction += '\n\nComplete admitted task and feedback data (also saved in operation.json):\n' + json.dumps(inputs, ensure_ascii=False)
    write_json(directory / 'initial-input-measurement.json', {'utf8_bytes': len(instruction.encode('utf-8')),
               'complete_feedback_embedded': True, 'feedback_truncated': False})
    command = sandbox_command(configuration['dsh_command'], candidate=candidate, materials=materials,
                              output=output, mutable=kind == 'generate_candidate')
    run = solve(workspace=candidate, target=PROJECT / 'harness/meta', output=output, command=command,
                instruction=instruction, timeout=configuration.get('meta_timeout_seconds', 900), external_isolation=True)
    write_json(directory / 'execution.json', run)
    if run['status'] != 'completed' or run['tool_calls'] < 1:
        raise BridgeError('meta_execution_failed', '改进 Agent 未完成真实工具执行；保留完整记录')
    data = meta_result(result_path, task_ids=task_ids)
    changes = candidate_changes(before, candidate)
    if kind == 'diagnose_seed' and changes:
        raise BridgeError('diagnosis_changed_candidate', '只读诊断操作修改了候选')
    receipt_candidate = None
    if kind == 'generate_candidate' and not data['declined']:
        if not changes:
            raise BridgeError('empty_candidate', '没有实际文件修改，不能生成候选')
        check_script = PROJECT / 'tools/check-target.mjs'
        checked = execute([configuration['dsh_command'][0], str(check_script), str(candidate)], timeout=60)
        (directory / 'target-check.stdout.txt').write_bytes(checked.stdout)
        (directory / 'target-check.stderr.txt').write_bytes(checked.stderr)
        if checked.returncode:
            raise BridgeError('candidate_check_failed', '候选配置未通过原生加载合同检查')
        cid = identifier(operation['candidate_id'])
        destination = Path(context['targets']) / cid
        if destination.exists():
            raise BridgeError('candidate_exists', '候选目录已经存在')
        shutil.copytree(candidate, destination)
        receipt_candidate = {'id': cid, 'parent_id': parent_id, 'digest': digest(inventory(destination))}
    write_json(directory / 'meta-result.json', data)
    receipt = response(request, candidate=receipt_candidate, data={**data, 'changed_files': changes},
                    calls=run['model_requests'], input_tokens=run['input_tokens'], output_tokens=run['output_tokens'],
                    provenance={'kind': 'real-dsh-meta', 'partition': 'seed', 'execution': run,
                                'changed_files': changes, 'evidence': 'complete-admitted-seed-input'})
    if kind == 'generate_candidate' and data['declined']:
        receipt['result']['status'] = 'failed'
    return receipt
