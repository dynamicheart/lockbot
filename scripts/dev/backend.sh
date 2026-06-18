#!/bin/bash
# Start backend only (foreground). Run in a dedicated terminal.
# Logs: stdout + logs/backend.log

set -e
cd "$(dirname "$0")/../.."

export DEV_MODE=true
mkdir -p logs

echo "==> Backend: http://0.0.0.0:8002"
echo "    Swagger: http://localhost:8002/docs"
echo "    Log:     logs/backend.log"
echo ""

uvicorn lockbot.backend.app.main:app \
    --host 0.0.0.0 \
    --port 8002 \
    --reload \
    --reload-dir python/ \
    --reload-delay 1 2>&1 | tee logs/backend.log
