#!/usr/bin/env bash
# start.sh --background 로 띄운 경우 종료
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
if [ -f subscriber.pid ]; then
  PID=$(cat subscriber.pid)
  kill "$PID" 2>/dev/null && echo "[SUBSCRIBER] 종료 PID=$PID" || echo "[SUBSCRIBER] 이미 종료됨 또는 권한 없음"
  rm -f subscriber.pid
else
  echo "[SUBSCRIBER] subscriber.pid 없음 (foreground로 실행 중이었을 수 있음)"
fi
