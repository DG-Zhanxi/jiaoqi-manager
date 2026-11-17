#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export JIAOQI_ROOT="$ROOT"
mkdir -p "$ROOT/runtime/logs" "$ROOT/runtime/tmp" "$ROOT/secrets"
if [ -f "$ROOT/runtime/cloud.env" ]; then
  set -a
  source "$ROOT/runtime/cloud.env"
  set +a
fi
export MOONSHOT_API_KEY_FILE="$ROOT/secrets/moonshot.key"
export PYTHONIOENCODING=utf-8
export TMPDIR="$ROOT/runtime/tmp"
PORT="${JIAOQI_PORT:-8788}"
if [ -f "$ROOT/runtime/cloud.pid" ]; then
  PID="$(cat "$ROOT/runtime/cloud.pid")"
  if kill -0 "$PID" 2>/dev/null; then
    echo "Existing process $PID; inspect before restarting."
    exit 0
  fi
fi
if ss -ltn | awk '{print $4}' | grep -Eq ":${PORT}$"; then
  echo "Port $PORT is occupied; choose an unused port in runtime/cloud.env."
  exit 1
fi
cd "$ROOT/app"
nohup "$ROOT/runtime/linux/bin/python" -m uvicorn backend.server:app --host 0.0.0.0 --port "$PORT" > "$ROOT/runtime/logs/cloud.log" 2>&1 &
echo $! > "$ROOT/runtime/cloud.pid"
for attempt in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:$PORT/api/health"; then
    printf '\nApplication started on port %s\n' "$PORT"
    exit 0
  fi
  sleep 1
done
cat "$ROOT/runtime/logs/cloud.log"
exit 1
