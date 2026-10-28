"""Candidate mutation and result validation shared by the actual DSH Meta runner."""
from __future__ import annotations

from pathlib import Path
from .common import BridgeError, inventory, read_json

MUTABLE = frozenset({'preset', 'plugins', 'prompts', 'skills', 'workflows'})
PROTECTED = frozenset({'manifest.json', 'package.json', 'package-lock.json', 'pnpm-lock.yaml',
                       'yarn.lock', 'bun.lock', 'bun.lockb', '.git'})


def result_contract(task_ids):
    """The result file protocol, including the exact admitted task vocabulary."""
    return {'type': 'object', 'required': ['summary', 'declined'], 'properties': {
        'summary': {'type': 'string', 'minLength': 1,
                    'description': 'Actual investigation, edits, loading checks and remaining uncertainty.'},
        'declined': {'type': 'boolean'},
        'hypotheses': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}},
        'clusters': {'type': 'array', 'description': 'Optional task groups; use [] when not grouping.',
            'items': {'type': 'object', 'required': ['task_ids'], 'properties': {
                'task_ids': {'type': 'array', 'items': {'type': 'string', 'enum': sorted(task_ids)}}}}}}}


def result_example(task_ids):
    return {'summary': 'Describe the investigation and checks actually performed.', 'declined': False,
            'hypotheses': ['A reusable mechanism supported by the supplied evidence.'],
            'clusters': [{'task_ids': sorted(task_ids)}] if task_ids else []}


def candidate_changes(before, directory):
    after = {row['path']: row for row in inventory(directory)}
    before = {row['path']: row for row in before}
    changed = []
    for name in sorted(before.keys() | after.keys()):
        if before.get(name) == after.get(name):
            continue
        parts = Path(name).parts
        if parts[0] not in MUTABLE or any(part in PROTECTED for part in parts):
            raise BridgeError('mutation_forbidden', '候选修改越过原生允许的配置范围：' + name)
        changed.append(name)
    return changed


def meta_result(path, *, task_ids):
    value = read_json(path)
    if not isinstance(value, dict) or type(value.get('declined')) is not bool:
        raise BridgeError('invalid_meta_result', '改进 Agent 未给出有效的完成状态')
    if not isinstance(value.get('summary'), str) or not value['summary'].strip():
        raise BridgeError('invalid_meta_result', '改进 Agent 缺少实际工作说明')
    hypotheses = value.get('hypotheses', [])
    if not isinstance(hypotheses, list) or any(not isinstance(v, str) or not v.strip() for v in hypotheses):
        raise BridgeError('invalid_meta_result', '诊断假设格式无效')
    clusters = value.get('clusters', [])
    if not isinstance(clusters, list):
        raise BridgeError('invalid_meta_result', '任务分组格式无效')
    normalizations = []
    for row in clusters:
        if not isinstance(row, dict):
            raise BridgeError('invalid_meta_result', '任务分组必须是对象')
        if 'affected_tasks' in row:
            if 'task_ids' in row and row['task_ids'] != row['affected_tasks']:
                raise BridgeError('invalid_meta_result', '任务分组的 task_ids 与 affected_tasks 冲突')
            if 'task_ids' not in row:
                row['task_ids'] = row['affected_tasks']
                normalizations.append('clusters[].affected_tasks -> task_ids')
        if not isinstance(row.get('task_ids'), list):
            raise BridgeError('invalid_meta_result', '任务分组缺少有效的 task_ids 列表')
        if any(not isinstance(v, str) or v not in task_ids for v in row['task_ids']):
            raise BridgeError('invalid_meta_result', '改进结果引用了未授权任务')
    if normalizations:
        value['format_normalizations'] = normalizations
    return {**value, 'hypotheses': hypotheses, 'clusters': clusters}
