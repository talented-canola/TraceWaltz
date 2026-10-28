#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
project="$PWD"
export PATH="$project/.runtime/node-v24.15.0-linux-x64/bin:$PATH"
export npm_config_cache="$project/.runtime/cache/linux-npm" PIP_CACHE_DIR="$project/.runtime/cache/linux-pip"
export TMPDIR="$project/.runtime/tmp"
if [ ! -d .runtime/hitch-source/.git ]; then
  git clone --filter=blob:none --no-checkout https://github.com/rsi-gear/agent-hitch.git .runtime/hitch-source
  git -C .runtime/hitch-source checkout --detach 6385c22388e69a3df0832cd88f21500a525751ee
fi
test "$(git -C .runtime/hitch-source rev-parse HEAD)" = 6385c22388e69a3df0832cd88f21500a525751ee
(cd .runtime/hitch-source && npm ci --ignore-scripts --no-audit --no-fund && npm run build)
mkdir -p .state/hitch
export HITCH_ROOT="$project/.state/hitch"
node .runtime/hitch-source/dist/bin/hitch.js --root "$HITCH_ROOT" eval setup harbor \
  --version 0.21.0 --python "$project/.runtime/linux-python/bin/python" --json
