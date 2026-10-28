"""Lifecycle for the project-owned Hitch daemon, using only the metered token."""
from __future__ import annotations
import json
import os
import time
from pathlib import Path

from .common import BridgeError, digest, execute, read_json, write_json


def daemon_environment(project, root):
    """Only explicit project runtime inputs enter Hitch's resident binding."""
    project, root = Path(project).resolve(), Path(root).resolve()
    temp = root / 'runtime-temp'
    home = project / '.runtime/hitch-home'
    cache = project / '.runtime/hitch-cache'
    for path in (temp, home, cache):
        path.mkdir(parents=True, exist_ok=True)
    environment = {
        'PATH': os.pathsep.join(str(project / path) for path in (
            'tools', '.runtime/docker', '.runtime/linux-tools/usr/bin',
            '.runtime/node-v24.15.0-linux-x64/bin', '.runtime/linux-python/bin')) + ':/usr/local/bin:/usr/bin:/bin',
        'HOME': str(home), 'XDG_CACHE_HOME': str(cache), 'XDG_CONFIG_HOME': str(home / '.config'),
        'TMPDIR': str(temp), 'TMP': str(temp), 'TEMP': str(temp), 'PWD': str(project),
        'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'PYTHONUTF8': '1', 'PYTHONNOUSERSITE': '1',
        'PIP_CACHE_DIR': str(project / '.runtime/pip-cache'),
        'npm_config_cache': str(project / '.runtime/npm-cache'), 'npm_config_prefer_offline': 'true',
        'LD_LIBRARY_PATH': str(project / '.runtime/linux-tools/usr/lib/x86_64-linux-gnu'),
        'DOCKER_HOST': os.environ.get('DOCKER_HOST', 'unix:///tmp/tracewaltz-docker.sock'),
        'DOCKER_CONFIG': str(project / '.runtime/docker-cli'), 'HITCH_ROOT': str(root), 'HITCH_HOME': str(root),
        'HITCH_HARBOR_BUILDER_BASE_IMAGE': os.environ.get('HITCH_HARBOR_BUILDER_BASE_IMAGE', 'tracewaltz-node22-builder:integration'),
    }
    for key in ('DEEPSEEK_API_KEY', 'DEEPSEEK_BASE_URL'):
        if key in os.environ:
            environment[key] = os.environ[key]
    return environment


def ensure_hitch(config):
    project = Path(__file__).resolve().parents[1]
    root = Path(config.get('execution', {}).get('hitch_root') or Path(config['data_root']) / 'hitch')
    command = [str(project / 'tools/hitch'), '--root', str(root)]
    capacity = {'cpu_millis': 4000, 'memory_mib': 4096, 'container_slots': 3, 'build_slots': 1,
                **config.get('execution', {}).get('worker_capacity', {})}
    identity_path = root / 'tracewaltz-environment.json'
    environment = daemon_environment(project, root)
    identity = digest({'launch_environment': environment,
                       'hitch_entry': config.get('execution', {}).get('hitch_executable'), 'capacity': capacity})
    status = execute([*command, 'daemon', 'status', '--json'], timeout=60)
    if status.returncode == 0 and json.loads(status.stdout).get('status') == 'running':
        if identity_path.exists() and read_json(identity_path)['digest'] == identity:
            return
        health = json.loads(status.stdout)
        if any((health.get(k) or {}).get('running', 0) or (health.get(k) or {}).get('queued', 0)
               for k in ('scheduler', 'eval_scheduler', 'eval_rerun_scheduler')):
            raise BridgeError('hitch_busy', 'Hitch 正在执行任务，不能更换本轮计量连接')
        stopped = execute([*command, 'daemon', 'stop'], timeout=60)
        if stopped.returncode:
            raise BridgeError('hitch_restart_failed', '未能停止项目原有空闲后台服务')
        time.sleep(2)
    started = execute([*command, 'daemon', 'start', '--port', '17979', '--max-concurrent', '1',
                       '--capacity-cpu-millis', str(capacity['cpu_millis']), '--capacity-memory-mib', str(capacity['memory_mib']),
                       '--container-slots', str(capacity['container_slots']), '--build-slots', str(capacity['build_slots'])],
                      cwd=project, env=environment, timeout=90)
    logs = project / 'artifacts/full-integration/environment'
    logs.mkdir(parents=True, exist_ok=True)
    (logs / 'hitch-start.txt').write_bytes(started.stdout + started.stderr)
    # On a cold DrvFS start the daemon can become ready after the CLI's own wait.
    deadline = time.monotonic() + config.get('execution', {}).get('daemon_startup_timeout_seconds', 120)
    while True:
        status = execute([*command, 'daemon', 'status', '--json'], timeout=30)
        if status.returncode == 0 and json.loads(status.stdout).get('status') == 'running':
            write_json(logs / 'hitch-daemon.json', json.loads(status.stdout))
            write_json(identity_path, {'digest': identity, 'credential': 'metered-gateway-token-only'})
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(2)
    raise BridgeError('hitch_not_ready', 'Hitch 后台服务未就绪，启动日志已保存')
