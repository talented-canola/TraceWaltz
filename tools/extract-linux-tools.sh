#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/../.runtime/linux-tools/debs"
for package in *.deb; do dpkg-deb -x "$package" ..; done
export LD_LIBRARY_PATH="$(realpath ../usr/lib/x86_64-linux-gnu):${LD_LIBRARY_PATH:-}"
../usr/bin/bwrap --ro-bind / / --unshare-net /usr/bin/true
../usr/bin/socat -V | head -2
../usr/bin/rg --version | head -1
