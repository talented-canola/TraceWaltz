"""Standalone Harbor wrapper preserving the original EvoTrace verifier and full outputs."""
from __future__ import annotations
import argparse
import contextlib
import fnmatch
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tarfile
import tempfile

IGNORED = {'.git', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', '.venv', 'node_modules'}


def extract(path, destination):
    if not path.exists() or not path.stat().st_size:
        return
    with tarfile.open(path) as archive:
        for row in archive.getmembers():
            if row.name.startswith('/') or '..' in row.name.split('/') or not (row.isfile() or row.isdir()):
                raise ValueError('unsafe source archive')
        archive.extractall(destination, filter='data')


def visible_files(directory):
    rows = {}
    for path in Path(directory).rglob('*'):
        relative = path.relative_to(directory)
        if set(relative.parts) & IGNORED:
            continue
        if path.is_symlink():
            raise ValueError('linked candidate artifact')
        if path.is_file():
            rows[relative.as_posix()] = path
    return rows


def command(argv, cwd):
    result = subprocess.run(argv, cwd=cwd, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8', 'replace'))


def original_report(source, workspace, logs):
    """Execute unchanged verifier code; record subprocess results before its display truncation."""
    outputs = []
    real_run = subprocess.run
    def recorded(*args, **kwargs):
        result = real_run(*args, **kwargs)
        text = result.stdout or ''
        if isinstance(text, bytes):
            text = text.decode('utf-8', 'replace')
        outputs.append({'command': args[0] if args else kwargs.get('args'),
                        'exit_code': result.returncode, 'output': text})
        return result
    stream = io.StringIO()
    previous = sys.argv
    try:
        subprocess.run = recorded
        sys.argv = [str(source / 'verifier.py'), '--repo', str(workspace), '--json']
        with contextlib.redirect_stdout(stream):
            try:
                runpy.run_path(str(source / 'verifier.py'), run_name='__main__')
            except SystemExit as exc:
                if exc.code not in (0, 1):
                    raise
    finally:
        subprocess.run = real_run
        sys.argv = previous
        (logs / 'command-outputs.json').write_text(json.dumps(outputs, ensure_ascii=False), encoding='utf-8')
        with (logs / 'test-stdout.txt').open('w', encoding='utf-8') as f:
            for row in outputs:
                f.write('COMMAND ' + str(row['command']) + '\n' + row['output'] + '\n')
    return json.loads(stream.getvalue())


def grade(source, candidate, logs, *, dependency_root=None, scratch=None):
    source, candidate, logs = Path(source), Path(candidate), Path(logs)
    logs.mkdir(parents=True, exist_ok=True)
    for name in ('reward.txt', 'reward.json', 'process.json', 'feedback.json'):
        (logs / name).unlink(missing_ok=True)
    original_path = os.environ.get('PATH', '')
    original_workspaces = {key: os.environ.get(key) for key in ('EVOTRACE_WORKSPACE', 'SCALEVERIFIER_WORKSPACE')}
    try:
        spec = json.loads((source / 'verifier.json').read_text())
        if not spec.get('commands'):
            raise ValueError('no behavioral verification commands')
        if not candidate.is_dir():
            raise ValueError('candidate artifacts are missing')
        with tempfile.TemporaryDirectory(prefix='tracewaltz-verify-', dir=scratch or logs) as temporary:
            workspace = Path(temporary)
            os.environ.update(EVOTRACE_WORKSPACE=str(workspace), SCALEVERIFIER_WORKSPACE=str(workspace))
            extract(source / 'base.tar.gz', workspace)
            for args in (['init', '-q'], ['config', 'core.autocrlf', 'false'], ['config', 'user.email', 'evotrace@local'],
                         ['config', 'user.name', 'EvoTrace'], ['add', '-A'], ['commit', '-q', '--allow-empty', '-m', 'EvoTrace base state']):
                command(['git', *args], workspace)
            initial = source / 'initial.patch'
            if initial.stat().st_size:
                command(['git', 'apply', '--whitespace=nowarn', str(initial)], workspace)
            extract(source / 'untracked-initial.tar.gz', workspace)
            initial_files = visible_files(workspace)
            hidden = source / 'hidden-tests.patch'
            if hidden.exists() and hidden.stat().st_size:
                command(['git', 'apply', '--whitespace=nowarn', str(hidden)], workspace)
                command(['git', 'add', '-A'], workspace)
                command(['git', 'commit', '-q', '--allow-empty', '-m', 'EvoTrace hidden verifier fixtures'], workspace)
            frozen_hidden = set(visible_files(workspace)) - set(initial_files)
            contract = source / 'protected-paths.json'
            protected = json.loads(contract.read_text()) if contract.exists() else spec.get('protected_paths', [])
            protected_hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest()
                for name, path in visible_files(workspace).items()
                if any(fnmatch.fnmatch(name, pattern) or name == pattern.rstrip('/')
                       or name.startswith(pattern.rstrip('/') + '/') for pattern in protected)}
            actual = visible_files(candidate)
            reserved_paths = sorted(set(actual) & frozen_hidden)
            for name in initial_files.keys() - actual.keys():
                (workspace / name).unlink()
            for name, path in actual.items():
                if name in frozen_hidden:
                    continue
                target = workspace / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
            if dependency_root:
                for name in ('node_modules', '.venv'):
                    path = Path(dependency_root) / name
                    if path.is_dir() and not (workspace / name).exists():
                        (workspace / name).symlink_to(path, target_is_directory=True)
                if (workspace / '.venv/bin').is_dir():
                    os.environ['PATH'] = str(workspace / '.venv/bin') + os.pathsep + original_path
            report = original_report(source, workspace, logs)
            rubric_file = source / 'behavior.json'
            components = []
            if rubric_file.exists():
                rubric = json.loads(rubric_file.read_text())
                for row in rubric['components']:
                    try:
                        result = subprocess.run(row['command'], shell=True, cwd=workspace, stdout=subprocess.PIPE,
                                                stderr=subprocess.STDOUT, timeout=spec['timeout_seconds'], check=False)
                        full = result.stdout.decode('utf-8', 'replace')
                        passed = result.returncode == 0
                    except subprocess.TimeoutExpired as exc:
                        full = (exc.stdout or b'').decode('utf-8', 'replace') + '\nBehavior command timed out.\n'
                        passed = False
                    with (logs / 'test-stdout.txt').open('a', encoding='utf-8') as f:
                        f.write('\nBEHAVIOR ' + row['id'] + '\n' + full)
                    components.append({'id': row['id'], 'category': 'behavior',
                                       'status': 'passed' if passed else 'failed', 'weight': row['weight']})
            else:
                components = [{'id': 'check-' + str(i), 'category': 'verifier',
                               'status': 'passed' if row['passed'] else 'failed', 'weight': 1}
                              for i, row in enumerate(report['checks'])]
            denominator = sum(row['weight'] for row in components)
            if not denominator:
                raise ValueError('no scored components')
            score = sum(row['weight'] for row in components if row['status'] == 'passed') / denominator
            protected_changes = [name for name, expected in protected_hashes.items()
                if not (workspace / name).is_file() or hashlib.sha256((workspace / name).read_bytes()).hexdigest() != expected]
            integrity = (not protected_changes and not reserved_paths and
                         all(row['passed'] for row in report['checks'] if row['name'] == 'protected tests were not modified'))
            reward = score if integrity else 0.0
            process = {'schema_version': '1', 'metric': 'behavior' if rubric_file.exists() else 'verifier_checks',
                       'score': score, 'detail_status': 'components',
                       'passed': sum(row['status'] == 'passed' for row in components), 'total': len(components),
                       'excluded': 0, 'components': components}
            feedback = {'schema_version': '1', 'items': [
                {'code': row['id'], 'severity': 'error', 'message': 'Check failed: ' + row['id'], 'component_ids': [row['id']]}
                for row in components if row['status'] == 'failed']}
            if not integrity:
                feedback['items'].append({'code': 'protected_tests_modified', 'severity': 'error',
                    'message': 'Protected verifier material changed or reserved private paths were supplied.'})
            for check in report['checks']:
                if not check['passed']:
                    feedback['items'].append({'code': 'original_verifier_failed', 'severity': 'error',
                        'message': 'Original verifier check failed: ' + check['name']})
            result = {'status': 'valid', 'passed': report['passed'] and integrity, 'score': reward,
                      'protected_test_changes': protected_changes, 'reserved_paths_supplied': reserved_paths,
                      'original_report': report, 'test_integrity': integrity, 'process': process}
            (logs / 'process.json').write_text(json.dumps(process), encoding='utf-8')
            (logs / 'feedback.json').write_text(json.dumps(feedback), encoding='utf-8')
            (logs / 'reward.json').write_text(json.dumps({'reward': reward, 'total_score': reward, 'process_score': score,
                'strict_pass': int(result['passed'])}), encoding='utf-8')
    except Exception as exc:
        result = {'status': 'scoring_failed', 'reason': type(exc).__name__ + ': ' + str(exc)}
    finally:
        os.environ['PATH'] = original_path
        for key, value in original_workspaces.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    (logs / 'evotrace-result.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=Path('/tests/source'))
    parser.add_argument('--candidate', type=Path, default=Path('/app'))
    parser.add_argument('--logs', type=Path, default=Path('/logs/verifier'))
    args = parser.parse_args()
    # Harbor replaces /app with submitted artifacts. Image-installed dependencies
    # remain outside that upload and are never taken from the candidate.
    result = grade(args.source, args.candidate, args.logs, dependency_root=Path('/opt/tracewaltz-dependencies'), scratch=Path('/tmp'))
    print(json.dumps(result, ensure_ascii=False))
    output = args.logs / 'test-stdout.txt'
    if output.exists():
        print(output.read_text(encoding='utf-8'))
    return 0 if result['status'] == 'valid' else 2


if __name__ == '__main__':
    raise SystemExit(main())
