#!/bin/bash
# Run in an existing Ubuntu 24.04 / WSL with python3-venv, Git, curl and xz available.
# Installs application files inside this project. No apt install, sudo, or Docker.
set -euo pipefail
cd "$(dirname "$0")/.."
project="$PWD"
mkdir -p .runtime/cache .runtime/tmp
export npm_config_cache="$project/.runtime/cache/linux-npm"
export PIP_CACHE_DIR="$project/.runtime/cache/linux-pip"
export TMPDIR="$project/.runtime/tmp"
node_version=v24.15.0
if [ ! -x ".runtime/node-$node_version-linux-x64/bin/node" ]; then
  curl --fail --location "https://nodejs.org/dist/$node_version/node-$node_version-linux-x64.tar.xz" -o .runtime/node-linux.tar.xz
  curl --fail --location "https://nodejs.org/dist/$node_version/SHASUMS256.txt" -o .runtime/node-SHASUMS256.txt
  expected=$(awk -v f="node-$node_version-linux-x64.tar.xz" '$2==f {print $1}' .runtime/node-SHASUMS256.txt)
  printf '%s  %s\n' "$expected" .runtime/node-linux.tar.xz | sha256sum --check
  tar -xJf .runtime/node-linux.tar.xz -C .runtime
fi
export PATH="$project/.runtime/node-$node_version-linux-x64/bin:$PATH"
if [ ! -d .runtime/gear-source/.git ]; then
  git clone --filter=blob:none --no-checkout https://github.com/rsi-gear/gear.git .runtime/gear-source
  git -C .runtime/gear-source checkout --detach c3809b95ce27bbc9420cc3836cbb692097ff9f1e
fi
test "$(git -C .runtime/gear-source rev-parse HEAD)" = c3809b95ce27bbc9420cc3836cbb692097ff9f1e
(cd .runtime/gear-source && npm ci --ignore-scripts --no-audit --no-fund)
node tools/build-gear.mjs
npm install --prefix .runtime/linux-dsh --ignore-scripts --no-audit --no-fund @deepseek-ai/dsh@0.1.5-rc.3
python3 -m venv .runtime/linux-python
evotrace_source="${EVOTRACE_SOURCE:-$project/.runtime/evotrace-source}"
if [ ! -d "$evotrace_source/.git" ]; then
  git clone --filter=blob:none --no-checkout https://github.com/jinzijian/EvoTrace.git "$evotrace_source"
  git -C "$evotrace_source" checkout --detach c96938020e762266a4b68e7b1c5df33f8de768dd
fi
test "$(git -C "$evotrace_source" rev-parse HEAD)" = c96938020e762266a4b68e7b1c5df33f8de768dd
patch="$project/patches/evotrace/integration.patch"
if git -C "$evotrace_source" apply --check "$patch"; then
  git -C "$evotrace_source" apply "$patch"
else
  git -C "$evotrace_source" apply --reverse --check "$patch"
fi
.runtime/linux-python/bin/python -m pip install -e .runtime/gear-source/python -e "$evotrace_source" -e .
mkdir -p .runtime/linux-tools/debs
(cd .runtime/linux-tools/debs && apt download bubblewrap=0.9.0-1build1 socat=1.8.0.0-4ubuntu0.1 ripgrep=14.1.0-1 libwrap0=7.6.q-33)
bash tools/extract-linux-tools.sh
echo 'Core runtime prepared. Continue with docs/INSTALL.md.'
