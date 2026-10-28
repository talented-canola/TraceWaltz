#!/bin/bash
set -euo pipefail
project="$(cd "$(dirname "$0")/.." && pwd)"
exec "$project/.runtime/linux-python/bin/python" -m evotrace_bridge.process_provider
