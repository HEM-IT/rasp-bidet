#!/usr/bin/env bash
# 라즈베리파이에서 MQTT Subscriber 구동
# 사용: ./start.sh          (foreground) 또는 ./start.sh --background

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$SCRIPT_DIR"

# .env 있으면 로드 (MQTT_URL, DEVICE_ID 등) — mqtt_subscriber/.env
if [ -f .env ]; then
  set -a
  # shellcheck source=/dev/null
  . .env
  set +a
fi
# 프로젝트 루트 .env 도 로드 (GPIO_SIMULATION=1 등 루트에서만 둔 경우)
if [ -f "$ROOT_DIR/.env" ]; then
  set -a
  # shellcheck source=/dev/null
  . "$ROOT_DIR/.env"
  set +a
fi

# 프로젝트 루트 .venv 가 있으면 자동으로 사용 (GPIO_VENV 미설정 시)
if [ -z "$GPIO_VENV" ] && [ -d "$ROOT_DIR/.venv" ]; then
  export GPIO_VENV="$ROOT_DIR/.venv"
fi

# 기기 번호: .env 의 DEVICE_ID 사용, 없으면 기본값
export DEVICE_ID="${DEVICE_ID:-EEEEE}"
# 테스트 서버: 52.78.222.49, 포트 1883
export MQTT_URL="${MQTT_URL:-mqtt://52.78.222.49:1883}"

# 테스트 단계 예시 값 (publisher 메시지에 없을 때 subscriber → gpio_controller 로 전달)
export TEST_GAS_ID="${TEST_GAS_ID:-EEEEE}"
export TEST_TEST_ID="${TEST_TEST_ID:-00000}"
export TEST_PROFILE_ID="${TEST_PROFILE_ID:-14}"

# 디바이스 상태 API 주소 (subscriber 기동 시 /device/status 등록용, 없으면 52.78.222.49:3001)
export DATA_API_URL="${DATA_API_URL:-http://52.78.222.49:3001}"

# 시뮬레이션 모드: .env 의 GPIO_SIMULATION=1 또는 MODE/TEST/ENV 로 설정 (gpio_controller에 전달됨)
if [ -n "$GPIO_SIMULATION" ]; then
  export GPIO_SIMULATION
elif [ -n "$MODE" ] && [ "$MODE" = "test" ]; then
  export GPIO_SIMULATION=1
elif [ -n "$TEST" ] && [ "$TEST" = "1" ]; then
  export GPIO_SIMULATION=1
elif [ -n "$ENV" ] && echo "$ENV" | grep -q "test"; then
  export GPIO_SIMULATION=1
fi

if [ "$1" = "--background" ]; then
  mkdir -p logs
  nohup node subscriber.js >> logs/subscriber.log 2>&1 &
  echo $! > subscriber.pid
  echo "[SUBSCRIBER] 백그라운드 기동 PID=$(cat subscriber.pid), 로그: logs/subscriber.log"
else
  exec node subscriber.js
fi
