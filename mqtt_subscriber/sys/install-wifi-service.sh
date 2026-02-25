#!/usr/bin/env bash
# hem-wifi 서비스를 systemd에 등록하고 부팅 시 자동 실행되도록 설정
# 사용: sudo ./install-wifi-service.sh  (mqtt_subscriber/sys/ 에서 실행)
# BASE_DIR = mqtt_subscriber 최상위 경로로 치환됨

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_NAME="hem-wifi.service"
SERVICE_FILE="$SCRIPT_DIR/$SERVICE_NAME"
SYSTEMD_DIR="/etc/systemd/system"

if [ "$(id -u)" -ne 0 ]; then
  echo "root로 실행하세요: sudo $0"
  exit 1
fi

# BASE_DIR 을 mqtt_subscriber 최상위 경로로 치환
sed "s|BASE_DIR|$BASE_DIR|g" "$SERVICE_FILE" > "$SYSTEMD_DIR/$SERVICE_NAME"
echo "[install] $SYSTEMD_DIR/$SERVICE_NAME 설치됨 (WorkingDirectory=$BASE_DIR)"

# 실행 권한
chmod +x "$SCRIPT_DIR/wifi-boot.sh"
chmod +x "$SCRIPT_DIR/wifi_check.sh"
chmod +x "$SCRIPT_DIR/ap_mode.sh"
chmod +x "$SCRIPT_DIR/sta_mode.sh"
chmod +x "$BASE_DIR/start.sh"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
echo "[install] 서비스 활성화됨. 부팅 시 자동 실행됩니다."
echo "[install] 수동 실행: sudo systemctl start $SERVICE_NAME"
echo "[install] 상태 확인: sudo systemctl status $SERVICE_NAME"
