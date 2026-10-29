#!/usr/bin/env bash
# Prepare dependencies only. Does not overwrite config, create keys, or start services.
set -euo pipefail
cd -- "$(dirname -- "$0")"
PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"'
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install --no-index --find-links wheels -r requirements.txt
./promtool check config examples/prometheus.example.yml
printf '%s\n' 'Dependencies ready. Follow AGENT-DEPLOY.md before enabling services.'
