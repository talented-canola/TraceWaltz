#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$PWD/.runtime/node-v24.15.0-linux-x64/bin:$PATH"
export npm_config_cache="$PWD/.runtime/npm-cache" TMPDIR="$PWD/.runtime/tmp"
mkdir -p .runtime/linux-review
"$PWD/.runtime/linux-python/bin/python" tools/pin-review-packages.py
npm install --prefix .runtime/linux-review --no-audit --no-fund --legacy-peer-deps --include=optional
