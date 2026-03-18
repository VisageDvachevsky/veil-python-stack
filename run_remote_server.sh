#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

export PYTHONPATH=.

exec python3 examples/local_chat.py \
  --mode server \
  --host 0.0.0.0 \
  --veil-port 4433 \
  --ui-host 0.0.0.0 \
  --ui-port 8080 \
  --name server
