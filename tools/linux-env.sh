#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$PWD/tools:$PWD/.runtime/docker:$PWD/.runtime/linux-tools/usr/bin:$PWD/.runtime/node-v24.15.0-linux-x64/bin:$PWD/.runtime/linux-python/bin:$PATH"
export LD_LIBRARY_PATH="$PWD/.runtime/linux-tools/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
export TMPDIR="$PWD/.runtime/tmp"
export TEMP="$TMPDIR" TMP="$TMPDIR" PYTHONUTF8=1
export DOCKER_HOST="${DOCKER_HOST:-unix:///tmp/tracewaltz-docker.sock}"
export DOCKER_CONFIG="${DOCKER_CONFIG:-$PWD/.runtime/docker-cli}"
export HITCH_HOME="${HITCH_HOME:-$PWD/.state/hitch}"
if [ -f "$PWD/.state/native-hitch-root" ]; then export HITCH_HOME=$(cat "$PWD/.state/native-hitch-root"); fi
export HITCH_ROOT="$HITCH_HOME"
export HITCH_HARBOR_BUILDER_BASE_IMAGE="${HITCH_HARBOR_BUILDER_BASE_IMAGE:-tracewaltz-node22-builder:integration}"
export PIP_CACHE_DIR="$PWD/.runtime/pip-cache"
export npm_config_cache="$PWD/.runtime/npm-cache" npm_config_prefer_offline=true
exec "$@"
