"""EvoTrace -> native Harbor, retaining upstream verifier, dependencies and private tests."""
from __future__ import annotations
import json
import math
import shutil
from pathlib import Path

from .common import (BridgeError, digest, extract, file_digest, git, identifier, init_git,
                     inventory, locked, read_json, staging, write_json)


def _dockerfile(environment):
    image = environment['base_image']
    if not isinstance(image, str) or any(c in image for c in '\r\n $;`'):
        raise BridgeError('invalid_image', '任务基底镜像需要先解析为明确引用')
    commands = environment.get('install_commands') or []
    install = '\n'.join('RUN ' + command.replace('/workspace', '/app') for command in commands)
    return f'''FROM {image}
RUN apt-get update && apt-get install -y --no-install-recommends git python3 python3-venv && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/evotrace-bootstrap
ENV PATH="/app/.venv/bin:/opt/evotrace-bootstrap/bin:${{PATH}}" PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1
WORKDIR /app
COPY bootstrap/base.tar.gz /tmp/base.tar.gz
RUN tar -xzf /tmp/base.tar.gz -C /app && git init -q && git config core.autocrlf false && git config user.email evotrace@local && git config user.name EvoTrace && git add -A && git commit -q --allow-empty -m 'EvoTrace base state'
COPY bootstrap/initial.patch /tmp/initial.patch
RUN if [ -s /tmp/initial.patch ]; then git apply --whitespace=nowarn /tmp/initial.patch; fi
COPY bootstrap/untracked-initial.tar.gz /tmp/untracked-initial.tar.gz
RUN if [ -s /tmp/untracked-initial.tar.gz ]; then tar -xzf /tmp/untracked-initial.tar.gz -C /app; fi
COPY bootstrap/task.md /opt/tracewaltz-task.md
{install}
RUN mkdir -p /opt/tracewaltz-dependencies && for d in .venv node_modules; do if [ -d "/app/$d" ]; then cp -a "/app/$d" "/opt/tracewaltz-dependencies/$d"; fi; done
ENV EVOTRACE_TASK_FILE=/opt/tracewaltz-task.md SCALEVERIFIER_TASK_FILE=/opt/tracewaltz-task.md EVOTRACE_WORKSPACE=/app SCALEVERIFIER_WORKSPACE=/app
CMD ["sleep", "infinity"]
'''


def _behavior(value):
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get('components'), list) or not value['components']:
        raise BridgeError('invalid_rubric', '行为评分需要明确的分项')
    seen = set()
    for row in value['components']:
        name = identifier(row['id'])
        weight = row.get('weight')
        if name in seen or type(weight) not in (int, float) or not math.isfinite(weight) or weight <= 0:
            raise BridgeError('invalid_rubric', '评分分项必须唯一且权重为正数')
        if not isinstance(row.get('command'), str) or not row['command'].strip():
            raise BridgeError('invalid_rubric', '评分分项缺少冻结的执行命令')
        seen.add(name)
    return value


def export_native(bundle, destination, *, family, split, behavior=None, calibration_version=None, products=None):
    from evotrace.catalog import bundle_sha256
    from evotrace.gitops import apply_patch, setup_from_bundle
    from . import native_verifier
    bundle, destination = Path(bundle).resolve(), Path(destination).resolve()
    family = identifier(family)
    if split not in {'seed', 'held_out'}:
        raise BridgeError('invalid_split', '任务需要明确用途分区')
    manifest = read_json(bundle / 'task.json')
    verifier = read_json(bundle / 'verifier.json')
    environment = read_json(bundle / 'environment/environment.json') if (bundle / 'environment/environment.json').exists() else manifest['environment']
    behavior = _behavior(behavior)
    instruction = manifest['task']
    calibration_selection = None
    if calibration_version:
        version = Path(calibration_version).resolve()
        report_path = version.parent.parent / 'calibration.json'
        if version.parent.name != 'versions' or not report_path.is_file():
            raise BridgeError('unbound_calibration', '标定版本缺少所属标定记录')
        report = read_json(report_path)
        if report.get('session_id') != manifest['id'] or report.get('bundle_sha256') != bundle_sha256(bundle):
            raise BridgeError('unbound_calibration', '标定版本不属于当前任务包')
        if not (version / 'version.json').is_file():
            raise BridgeError('unbound_calibration', '标定版本描述缺失')
        instruction = (version / 'task.md').read_text(encoding='utf-8')
        verifier = read_json(version / 'verifier.json')
        calibration_selection = {'calibration': report['id'], 'version': version.name,
                                 'version_digest': file_digest(version / 'version.json'),
                                 'report_digest': file_digest(report_path)}
    if not verifier.get('commands'):
        raise BridgeError('missing_verifier', '任务没有行为验证命令')
    protected = set(verifier.get('protected_paths') or [])
    lineage, seen = bundle, set()
    while True:
        ancestor = read_json(lineage / 'task.json').get('source') or {}
        parent_id = ancestor.get('parent_id')
        if not parent_id:
            break
        if parent_id in seen:
            raise BridgeError('invalid_lineage', '任务来源形成循环')
        seen.add(parent_id)
        parent = bundle.parent / identifier(parent_id)
        if not parent.is_dir():
            raise BridgeError('parent_contract_missing', '旧加难包缺少继承的测试保护信息，需要其原父任务包')
        if ancestor.get('parent_bundle_sha256') and ancestor['parent_bundle_sha256'] != bundle_sha256(parent):
            raise BridgeError('parent_contract_changed', '继承的测试保护不再对应原父任务')
        protected.update(read_json(parent / 'verifier.json').get('protected_paths') or [])
        lineage = parent
    request = {'source_bundle': bundle_sha256(bundle), 'family': family, 'split': split,
               'behavior': behavior, 'selected_instruction': instruction, 'selected_verifier': verifier,
               'environment': environment, 'exporter': file_digest(Path(__file__)),
               'protected_paths': sorted(protected),
               'verifier_wrapper': file_digest(Path(native_verifier.__file__))}
    if calibration_selection:
        request['calibration_selection'] = calibration_selection
    if products:
        if products.get('bundle_sha256') != request['source_bundle'] or products.get('task_id') != manifest['id']:
            raise BridgeError('unbound_products', '附属材料不属于当前任务版本')
        request['products'] = [{**row, 'files': inventory(row['path']) if Path(row['path']).is_dir()
                                else [{'path': Path(row['path']).name, 'digest': file_digest(row['path'])}]}
                               for row in products['products'] if row['kind'] != 'task_bundle']
    with locked(destination.parent, destination.name):
        if destination.exists():
            saved = verify_native(destination)
            if saved['request'] != request:
                raise BridgeError('native_export_conflict', '该目录绑定了不同来源或评分规格')
            return saved
        with staging(destination) as temp:
            if products:
                index = []
                for i, row in enumerate(request['products']):
                    source_path = Path(row['path'])
                    relative = 'product-evidence/' + str(i + 1) + '-' + row['kind']
                    destination_path = temp / relative
                    if source_path.is_dir():
                        shutil.copytree(source_path, destination_path)
                    else:
                        destination_path.mkdir(parents=True)
                        shutil.copyfile(source_path, destination_path / source_path.name)
                    index.append({**row, 'relative_path': relative})
                write_json(temp / 'product-evidence/index.json', {'products': index})
                measurements = []
                for product in index:
                    if product['kind'] == 'calibration':
                        calibrated = read_json(temp / product['relative_path'] / 'calibration.json')
                        measurements.append({'status': calibrated['status'], 'target': calibrated['target'],
                            'rounds': [{k: row[k] for k in ('version', 'pass_count', 'trial_count', 'verifier_commands')}
                                       for row in calibrated['rounds']]})
                write_json(temp / 'public-asset-profile.json', {'family': family,
                           'structural_difficulty': manifest.get('difficulty'), 'measurements': measurements,
                           'selected_calibration': calibration_selection})
            public, private = temp / 'environment', temp / 'tests'
            source = private / 'source'
            source.mkdir(parents=True)
            for folder in (public, private):
                bootstrap = folder / 'bootstrap'
                bootstrap.mkdir(parents=True)
                for name, path in [('base.tar.gz', bundle / 'environment/base.tar.gz'),
                                   ('initial.patch', bundle / 'patches/initial.patch'),
                                   ('untracked-initial.tar.gz', bundle / 'environment/untracked-initial.tar.gz')]:
                    shutil.copyfile(path, bootstrap / name)
                (bootstrap / 'task.md').write_text(instruction.strip() + '\n', encoding='utf-8')
                (folder / 'Dockerfile').write_text(_dockerfile(environment), encoding='utf-8', newline='\n')
            for name in ('base.tar.gz', 'initial.patch', 'untracked-initial.tar.gz'):
                shutil.copyfile(public / 'bootstrap' / name, source / name)
            shutil.copyfile(bundle / 'verifier.py', source / 'verifier.py')
            write_json(source / 'verifier.json', verifier)
            write_json(source / 'protected-paths.json', sorted(protected))
            hidden = bundle / 'patches/hidden-tests.patch'
            if hidden.exists():
                shutil.copyfile(hidden, source / 'hidden-tests.patch')
            if behavior:
                write_json(source / 'behavior.json', behavior)
            shutil.copyfile(Path(native_verifier.__file__), private / 'verify.py')
            (private / 'test.sh').write_text('#!/bin/sh\nset -eu\npython3 /tests/verify.py\n', encoding='utf-8', newline='\n')
            (private / 'test.sh').chmod(0o755)
            solution = temp / 'solution'
            solution.mkdir()
            # Reference applies to original Git base, not on top of initial.patch.
            shutil.copyfile(bundle / 'environment/base.tar.gz', solution / 'base.tar.gz')
            shutil.copyfile(bundle / 'patches/reference.patch', solution / 'reference.patch')
            shutil.copyfile(bundle / 'environment/untracked-reference.tar.gz', solution / 'untracked-reference.tar.gz')
            (solution / 'solve.sh').write_text('''#!/bin/sh
set -eu
cd /app
find . -mindepth 1 -maxdepth 1 ! -name .git ! -name .venv ! -name node_modules -exec rm -rf -- {} +
tar -xzf /solution/base.tar.gz -C /app
if [ -s /solution/reference.patch ]; then git apply --whitespace=nowarn /solution/reference.patch; fi
if [ -s /solution/untracked-reference.tar.gz ]; then tar -xzf /solution/untracked-reference.tar.gz -C /app; fi
''', encoding='utf-8', newline='\n')
            (solution / 'solve.sh').chmod(0o755)
            (temp / 'instruction.md').write_text(instruction.strip() + '\n\nWork in /app.\n', encoding='utf-8')
            (temp / 'task.toml').write_text('version = "1.0"\nartifacts = ["/app"]\n[metadata]\n'
                + 'family = ' + json.dumps(family) + '\nsplit = ' + json.dumps(split) + '\n'
                + '[agent]\ntimeout_sec = 900\n[verifier]\ntimeout_sec = 1200\nenvironment_mode = "separate"\n'
                + '[verifier.environment]\nnetwork_mode = "no-network"\n[environment]\nos = "linux"\n', encoding='utf-8')
            body = {'version': 2, 'format': 'tracewaltz-native-harbor', 'request': request,
                    'original_manifest': manifest, 'original_verifier_digest': file_digest(bundle / 'verifier.py'),
                    'files': inventory(temp)}
            write_json(temp / 'tracewaltz.asset.json', {**body, 'digest': digest(body)})
            if bundle_sha256(bundle) != request['source_bundle']:
                raise BridgeError('source_changed', '导出期间任务包发生变化')
    return verify_native(destination)


def verify_native(directory):
    directory = Path(directory)
    manifest = read_json(directory / 'tracewaltz.asset.json')
    body = {k: v for k, v in manifest.items() if k != 'digest'}
    if digest(body) != manifest['digest'] or inventory(directory, exclude={'tracewaltz.asset.json'}) != manifest['files']:
        raise BridgeError('native_asset_changed', '原生任务的文件或规格已改变')
    if file_digest(directory / 'tests/source/verifier.py') != manifest['original_verifier_digest']:
        raise BridgeError('original_verifier_changed', '上游验证器文件发生变化')
    return manifest


def freeze_native(assets, destination, *, set_id):
    assets = [Path(p).resolve() for p in assets]
    destination = Path(destination).resolve()
    identifier(set_id)
    entries, ids, sources, families, lineage_splits, instructions = [], set(), set(), {}, {}, {}
    manifests = [verify_native(p) for p in assets]
    parents = {m['original_manifest']['id']: m['original_manifest'].get('source', {}).get('parent_id') for m in manifests}
    for path, manifest in zip(assets, manifests):
        request = manifest['request']
        task_id = identifier(path.name)
        source = request['source_bundle']
        if task_id in ids or source in sources:
            raise BridgeError('duplicate_task', '同一任务不能重复进入本轮题集')
        ids.add(task_id)
        sources.add(source)
        family, split = request['family'], request['split']
        instruction_key = digest(request['selected_instruction'].strip())
        if instructions.setdefault(instruction_key, split) != split:
            raise BridgeError('split_leakage', '相同题目不能跨优化和留出分区')
        if families.setdefault(family, split) != split:
            raise BridgeError('split_leakage', '同一任务族不能跨优化和留出分区')
        original = manifest['original_manifest']
        lineage = original['id']
        seen = set()
        while parents.get(lineage):
            if lineage in seen:
                raise BridgeError('invalid_lineage', '任务来源形成循环')
            seen.add(lineage)
            lineage = parents[lineage]
        if lineage_splits.setdefault(lineage, split) != split:
            raise BridgeError('split_leakage', '原任务和派生题不能跨分区')
        entries.append({'task_id': task_id, 'family': family, 'split': split, 'source_digest': source,
                        'export_digest': manifest['digest'], 'lineage_root': lineage})
    if {row['split'] for row in entries} != {'seed', 'held_out'}:
        raise BridgeError('invalid_task_set', '需要优化题与独立留出题')
    body = {'version': 2, 'format': 'tracewaltz-native-harbor', 'set_id': set_id,
            'tasks': sorted(entries, key=lambda row: row['task_id'])}
    with locked(destination.parent, destination.name):
        if destination.exists():
            old = verify_native_set(destination)
            if old['digest'] != digest(body):
                raise BridgeError('set_frozen', '题集已经冻结，不能替换其中的题目')
            return old
        with staging(destination) as temp:
            for path in assets:
                shutil.copytree(path, temp / 'tasks' / path.name)
            write_json(temp / 'evaluation-set.json', {**body, 'digest': digest(body)})
    return verify_native_set(destination)


def verify_native_set(directory):
    directory = Path(directory)
    value = read_json(directory / 'evaluation-set.json')
    if digest({k: v for k, v in value.items() if k != 'digest'}) != value.get('digest'):
        raise BridgeError('set_changed', '题集规格已改变')
    if {p.name for p in (directory / 'tasks').iterdir()} != {r['task_id'] for r in value['tasks']}:
        raise BridgeError('set_changed', '冻结后增加或删除了题目')
    for row in value['tasks']:
        if verify_native(directory / 'tasks' / row['task_id'])['digest'] != row['export_digest']:
            raise BridgeError('set_changed', '题目内容发生变化')
    return value
