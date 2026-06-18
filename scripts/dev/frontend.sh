#!/bin/bash
# Start frontend only (foreground). Run in a dedicated terminal.
# Logs: stdout + logs/frontend.log

set -e
cd "$(dirname "$0")/../.."

mkdir -p logs

echo "==> Frontend: http://0.0.0.0:8001"
echo "    Proxy:    /api/* -> http://localhost:8002"
echo "    Log:      logs/frontend.log"
echo ""

cd frontend
npx vite --host 0.0.0.0 --port 8001 2>&1 | tee ../logs/frontend.log &
TEE_PID=$!
trap "kill $TEE_PID 2>/dev/null" EXIT
wait $TEE_PID
