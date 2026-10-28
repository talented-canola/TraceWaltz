"""Prepare native Gear/Hitch evaluation and a full DSH Meta process profile."""
from __future__ import annotations
import json
import shutil
from pathlib import Path

from .common import BridgeError, execute, extract, git, identifier, read_json, staging, write_json
from .gear import active_release
from .native_export import verify_native_set

PROJECT = Path(__file__).resolve().parents[1]


def native_configuration(config):
    options = config.get('optimization', {})
    score = {'id': 'task_score', 'revision': '1', 'unit': 'ratio', 'direction': 'maximize',
             'source': {'path': 'rewards.reward', 'extractor': 'number-v1'}, 'range': {'min': 0, 'max': 1},
             'granularity': 'trial', 'repetitionReducer': 'mean', 'taskReducer': 'weighted-mean', 'comparisonPrecision': 1e-9}
    strict = {**score, 'id': 'strict_pass_rate',
              'source': {'path': 'verifier.result.rewards.strict_pass', 'extractor': 'number-v1'}}
    return {'version': 1, 'benchmark': './benchmark.yaml',
            'target': {'adapter': 'dsh', 'path': './target', 'profile': 'coding-target'},
            'meta': {'profile': 'coding-meta'}, 'algorithm': 'gepa', 'metrics': [score, strict],
            'objective': {'terms': [{'metric': 'task_score', 'weight': 1}],
                          'constraints': [{'metric': 'strict_pass_rate', 'rule': 'no_regression', 'reference': 'initial_baseline'}]},
            'validation': {'mode': 'holdout'},
            'budget': {'rounds': options.get('rounds', 2), 'max_candidates': options.get('max_candidates', 4),
                       'max_evaluations': options.get('max_evaluations', 100), 'repetitions': options.get('repetitions', 2)}}


def prepare_native_optimization(config, dataset, request_id):
    frozen = verify_native_set(dataset)
    root = Path(config['data_root']) / 'optimizations' / identifier(request_id)
    blocked = set(config.get('blocked_source_digests', []))
    if any(row['source_digest'] in blocked for row in frozen['tasks']):
        raise BridgeError('retired_task', '该任务集含有已退役题目')
    if (root / 'request.json').exists():
        request = read_json(root / 'request.json')
        if request['dataset_digest'] != frozen['digest'] or request.get('backend') != 'hitch':
            raise BridgeError('operation_conflict', '优化请求绑定不同数据或执行方式')
        return root, request
    execution = config.get('execution', {})
    base_image = execution.get('base_image')
    if not base_image:
        observed = execute(['docker', 'image', 'inspect', 'python:3.11-slim', '--format', '{{index .RepoDigests 0}}'], timeout=30)
        base_image = observed.stdout.decode().strip()
        if observed.returncode or '@sha256:' not in base_image:
            raise BridgeError('image_not_prepared', '先准备并固定原生评测基底镜像')
    active = active_release(Path(config['data_root']) / 'releases')
    request = {'version': 2, 'backend': 'hitch', 'request_id': request_id, 'dataset': str(Path(dataset).resolve()),
               'dataset_digest': frozen['digest'], 'baseline_ref': active['config_ref'], 'configuration': native_configuration(config)}
    with staging(root) as temp:
        project = temp / 'gear-project'
        project.mkdir()
        shutil.copytree(Path(active['directory']) / 'target', project / 'target')
        # Gear's native importer writes benchmark.yaml after a frozen import preview.
        write_json(project / 'gear.yaml', request['configuration'])
        public = temp / 'public-materials'
        tasks = {}
        for row in frozen['tasks']:
            if row['split'] != 'seed':
                continue
            asset = Path(dataset) / 'tasks' / row['task_id']
            dest = public / 'tasks' / row['task_id']
            dest.mkdir(parents=True)
            shutil.copyfile(asset / 'instruction.md', dest / 'instruction.md')
            if (asset / 'public-asset-profile.json').is_file():
                shutil.copyfile(asset / 'public-asset-profile.json', dest / 'public-asset-profile.json')
            # Only the initial code and declared environment are public. No tests/source or solution is copied.
            shutil.copytree(asset / 'environment', dest / 'environment')
            # Readable public source complements the original archives; private
            # tests and reference code remain outside the Meta materials.
            initial = dest / 'initial-code'
            initial.mkdir()
            bootstrap = asset / 'environment/bootstrap'
            extract(bootstrap / 'base.tar.gz', initial)
            patch = bootstrap / 'initial.patch'
            if patch.stat().st_size:
                git(initial, 'apply', '--whitespace=nowarn', str(patch))
            extract(bootstrap / 'untracked-initial.tar.gz', initial)
            product_index = asset / 'product-evidence/index.json'
            if product_index.is_file():
                from .common import relative_path
                assessments = []
                for i, product in enumerate(read_json(product_index)['products']):
                    if product.get('audience') == 'seed_meta' and product['kind'] == 'compressed_experience':
                        source = asset / relative_path(product['relative_path'])
                        shutil.copytree(source, dest / 'experience' / str(i + 1))
                    if product['kind'] == 'experience_and_comparison':
                        report = read_json(asset / relative_path(product['relative_path']) / 'evolution.json')
                        assessments.append({'origin': 'repository exploration and paired comparison', **{key: report.get(key) for key in (
                            'protocol', 'status', 'utility', 'paired_evaluation', 'functional_compression_verified',
                            'boundary_audit', 'exploration', 'policy')}})
                    if product.get('audience') == 'seed_meta' and product['kind'] == 'recorded_experience_assessment':
                        report = read_json(asset / relative_path(product['relative_path']) / 'assessment.json')
                        assessments.append(report)
                    if product['kind'] == 'review_reports':
                        review_source = asset / relative_path(product['relative_path'])
                        review_destination = dest / 'review'
                        review_destination.mkdir()
                        for report in sorted(review_source.glob('*.md')):
                            shutil.copyfile(report, review_destination / report.name)
                        # The full state JSON includes private raw records. The
                        # original role explanations are provided as written.
                        write_json(review_destination / 'scope.json', {'source': 'original role reports',
                            'audience': 'optimization seed only', 'reference_patch_supplied': False})
                if assessments:
                    write_json(dest / 'experience/assessments.json', {
                        'scope': 'source-bound experience; each result declares its validation and comparison scope', 'results': assessments})
            tasks[row['task_id']] = {'directory': 'tasks/' + row['task_id'], 'split': 'seed'}
        write_json(public / 'index.json', {'version': 1, 'dataset_digest': frozen['digest'], 'tasks': tasks})
        target_profile = {'kind': 'hitch', 'executable': str(execution.get('hitch_executable') or PROJECT / '.runtime/hitch-source/dist/bin/hitch.js'),
                          'root': str(execution.get('hitch_root') or Path(config['data_root']) / 'hitch'),
                          'harnessRef': 'deepseek@version:' + config['coding_runtime']['version'],
                          'model': config['coding_runtime']['model'], 'baseImage': base_image,
                          'passEnv': ['DEEPSEEK_API_KEY', 'DEEPSEEK_BASE_URL'],
                          'timeoutMs': execution.get('task_timeout_ms', 900000), 'maxConcurrent': 1,
                          'setupTimeoutMs': 1800000, 'preparationTimeoutMs': 1800000}
        meta_profile = {'command': [str(PROJECT / 'tools/provider.sh')], 'workingDirectory': str(PROJECT),
                        'passEnv': ['PATH', 'TEMP', 'TMP', 'PYTHONUTF8', 'DEEPSEEK_BASE_URL', 'DEEPSEEK_API_KEY'],
                        'configuration': {'state_root': str(root / 'meta-provider'), 'meta_runtime': 'dsh',
                                          'dsh_command': config['coding_runtime']['command'],
                                          'dsh_version': config['coding_runtime']['version'],
                                          'public_materials': str(root / 'public-materials'),
                                          'hitch_root': target_profile['root'], 'hitch_command': [str(PROJECT / 'tools/hitch')],
                                          'require_complete_evidence': True, 'meta_timeout_seconds': 1200},
                        'timeoutMs': 1260000}
        write_json(temp / 'profiles/coding-target.json', target_profile)
        write_json(temp / 'profiles/coding-meta.json', meta_profile)
        write_json(temp / 'request.json', request)
    return root, request
