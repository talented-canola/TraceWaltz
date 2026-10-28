"""Project-owned read adapter for full admitted seed diagnostics and native sessions."""
from __future__ import annotations
import json
import hashlib
import shutil
from pathlib import Path

from .common import BridgeError, execute, gear_digest, identifier, read_json, relative_path, write_json

ARTIFACTS = frozenset({'ctrf.json', 'test-stdout.txt', 'test-stderr.txt', 'stdout.txt', 'stderr.txt'})


def public_details(value):
    if isinstance(value, list):
        return [public_details(v) for v in value]
    if isinstance(value, dict):
        return {k: public_details(v) for k, v in value.items() if k != 'private_details_ref'}
    return value


def _hitch(config, *args):
    result = execute([*config['hitch_command'], '--root', config['hitch_root'], *args], timeout=120)
    if result.returncode:
        raise BridgeError('evidence_read_failed', result.stderr.decode('utf-8', 'replace'))
    return json.loads(result.stdout)


def diagnostic(config, run_id, name, *, expected_digest=None):
    if name not in ARTIFACTS:
        raise BridgeError('diagnostic_not_public', '该材料不属于公开诊断接口')
    offset, pieces, source = 0, [], None
    while True:
        args = ['verifier', 'artifact', run_id, name, '--offset', str(offset), '--limit', '65536', '--json']
        if expected_digest:
            args += ['--sha256', expected_digest]
        value = _hitch(config, *args)
        page, artifact = value['page'], value['artifact']
        if value['run_id'] != run_id or artifact['name'] != name or page['offset'] != offset:
            raise BridgeError('diagnostic_changed', '评分诊断不再对应冻结的来源')
        if source is None:
            source = artifact
        elif source != artifact:
            raise BridgeError('diagnostic_changed', '读取期间评分诊断发生变化')
        pieces.append(page['text'])
        if page['eof']:
            break
        next_offset = page.get('next_offset')
        if type(next_offset) is not int or next_offset <= offset:
            raise BridgeError('diagnostic_cursor_invalid', '评分诊断分页没有推进')
        offset = next_offset
    if not source.get('source_complete', False):
        raise BridgeError('diagnostic_source_incomplete', '上游诊断捕获不完整，不能当作完整反馈')
    content = ''.join(pieces)
    raw = content.encode('utf-8')
    actual = hashlib.sha256(raw).hexdigest()
    if len(raw) != source['bytes'] or actual != source['sha256'].removeprefix('sha256:'):
        raise BridgeError('diagnostic_changed', '完整诊断内容与上游摘要不一致')
    if expected_digest and actual != expected_digest.removeprefix('sha256:'):
        raise BridgeError('diagnostic_changed', '评分诊断不属于本次冻结的来源')
    return {'artifact': source, 'text': content}


def _bound_sources(request, context):
    journal_path = Path(context['targets']).parent / 'journal.json'
    journal = read_json(journal_path)
    if journal.get('id') != request['runId'] or gear_digest({k: v for k, v in journal.items() if k != 'digest'}) != journal.get('digest'):
        raise BridgeError('evidence_journal_changed', '运行记录不属于当前改进操作或已发生变化')
    reservation = next((r for r in journal['operations'] if r['request']['operationId'] == request['operationId']), None)
    if reservation is None or gear_digest(reservation['request']) != gear_digest(request):
        raise BridgeError('evidence_operation_unbound', '改进操作不属于已保存的请求')
    seed_ids = {row['id'] for row in context['tasks']}
    sources = []
    for cell in request['evidence']:
        if cell['task_id'] not in seed_ids or cell.get('scope') == 'held_out':
            raise BridgeError('heldout_meta_forbidden', '反馈超出授权的优化题范围')
        matches = []
        for entry in journal['operations']:
            saved = entry['request']
            receipt = entry.get('receipt') or {}
            if entry['status'] != 'complete' or saved['partition'] != 'seed' or saved['operation']['kind'] != 'evaluate_seed':
                continue
            if not any(all(row.get(k) == cell.get(k) for k in ('candidate_id', 'task_id', 'repetition', 'score', 'status', 'scope'))
                       for row in receipt.get('evidence', [])):
                continue
            for source in (receipt.get('provenance') or {}).get('verifier_sources', []):
                if source['task_id'] == cell['task_id'] and source['repetition'] == cell['repetition']:
                    admitted = (cell.get('verifier') or {}).get('source_digest')
                    if admitted and source['observation'].get('source_digest') != admitted:
                        continue
                    if source['source'] is not None and source['observation'].get('source_digest') != gear_digest(source['source']):
                        raise BridgeError('verifier_source_changed', '完整评分来源与采用的观测不一致')
                    matches.append(source)
        if not matches:
            raise BridgeError('full_evidence_missing', '找不到这条优化反馈对应的完整评分来源')
        if len({gear_digest(row) for row in matches}) != 1:
            raise BridgeError('full_evidence_ambiguous', '优化反馈对应多个不同来源')
        sources.append((cell, matches[0]))
    return sources


def materialize(request, context, config, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    observations = []
    for index, (cell, source) in enumerate(_bound_sources(request, context)):
        run_id = identifier(source['run_id'])
        verifier = (source.get('source') or {}).get('verifier') or {}
        directory = destination / ('observation-' + str(index + 1))
        directory.mkdir()
        details = verifier.get('diagnostics') or {}
        available = []
        if details.get('ctrf'):
            available.append(details['ctrf'])
        available += details.get('stdout', []) + details.get('stderr', [])
        diagnostics = {}
        for row in available:
            name = row['name']
            if name in ARTIFACTS and name not in diagnostics:
                diagnostics[name] = diagnostic(config, run_id, name, expected_digest=row.get('sha256'))
                (directory / name).write_text(diagnostics[name]['text'], encoding='utf-8')
        native_root = Path(config['hitch_root']) / 'runs' / run_id
        native, canonical, transcripts = [], [], []
        reference = native_root / 'trajectory.ref.json'
        declared = read_json(reference) if reference.exists() else None
        if declared and declared.get('run_id') != run_id:
            raise BridgeError('native_trace_unbound', '原始会话清单不属于这次执行')
        files = declared.get('files', []) if declared else []
        for descriptor in files:
            if descriptor.get('role') not in {'provider_events', 'canonical_session', 'provider_transcript'}:
                continue
            relative = relative_path(descriptor['path'])
            path = native_root / relative
            if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(native_root.resolve()):
                raise BridgeError('native_trace_missing', '清单声明的原始会话缺失或路径无效')
            raw = path.read_bytes()
            if len(raw) != descriptor['bytes'] or 'sha256:' + hashlib.sha256(raw).hexdigest() != descriptor['sha256']:
                raise BridgeError('native_trace_changed', '原始会话内容与清单不一致')
            target = directory / 'sessions' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            if path.suffix == '.zstd':
                import zstandard
                with path.open('rb') as f, zstandard.ZstdDecompressor().stream_reader(f) as reader:
                    content = reader.read().decode('utf-8')
            else:
                content = path.read_text(encoding='utf-8')
            target.with_suffix('.decoded.jsonl').write_text(content, encoding='utf-8')
            row = {'path': target.relative_to(destination).as_posix(), 'content': content,
                   'role': descriptor['role'], 'sha256': descriptor['sha256']}
            channel = native if descriptor['role'] == 'provider_events' else canonical if descriptor['role'] == 'canonical_session' else transcripts
            channel.append(row)
        if declared and declared.get('fidelity') == 'provider_native' and not native:
            raise BridgeError('native_trace_incomplete', '上游声明保留原会话，但清单没有可读取的原事件')
        observation = {'task_id': cell['task_id'], 'candidate_id': cell['candidate_id'], 'repetition': cell['repetition'],
                       'score': cell['score'], 'process': public_details(verifier.get('process')),
                       'feedback': public_details(verifier.get('feedback')), 'diagnostics': diagnostics,
                       'verifier_capture': source['observation'],
                       'native_sessions': native, 'native_sessions_captured': bool(native),
                       'canonical_sessions': canonical, 'provider_transcripts': transcripts,
                       'trajectory_status': 'manifest_verified' if declared else 'upstream_manifest_unavailable'}
        write_json(directory / 'public-observation.json', observation)
        observations.append(observation)
    result = {'status': 'complete', 'partition': 'seed', 'scope': 'all_captured_public_records', 'observations': observations}
    write_json(destination / 'index.json', result)
    return result
