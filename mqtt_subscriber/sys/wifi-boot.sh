#!/usr/bin/env bash
# 라즈베리파이 부팅 후 등록 서비스 진입점
# 1) wifi-check → 연결되면 start.sh 실행
# 2) 연결 없으면 AP 모드 전환 후 Flask 설정 페이지 제공
# 3) 사용자가 SSID 등록 후 STA 전환 및 재부팅 → 다시 1번부터
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

export WPA_SUPPLICANT_CONF="${WPA_SUPPLICANT_CONF:-/etc/wpa_supplicant/wpa_supplicant.conf}"
export WIFI_INTERFACE="${WIFI_INTERFACE:-wlan0}"

# wifi-check
if "$SCRIPT_DIR/wifi_check.sh"; then
  echo "[wifi-boot] WiFi 연결됨 → start.sh 실행"
  exec "$BASE_DIR/start.sh" "$@"
fi

# WiFi 미연결 → (가능하면 스캔 캐시 저장) → AP 모드 전환 후 설정 웹 서버 기동
echo "[wifi-boot] WiFi 미연결 → AP 모드 전환 및 설정 페이지 기동"

# AP 전환 전에 WiFi 스캔 실행해 SSID 목록 캐시 (웹에서 검증용)
WIFI_SCAN_CACHE="$SCRIPT_DIR/.wifi_scan_cache"
if iwlist "$WIFI_INTERFACE" scan 2>/dev/null | grep -oP 'ESSID:"\K[^"]+' | sort -u > "$WIFI_SCAN_CACHE"; then
  echo "[wifi-boot] SSID 스캔 캐시 저장: $WIFI_SCAN_CACHE"
else
  : > "$WIFI_SCAN_CACHE"
fi

# AP 모드 전환 (sudo 필요; 서비스로 실행 시 root일 수 있음)
if [ "$(id -u)" -eq 0 ]; then
  "$SCRIPT_DIR/ap_mode.sh"
else
  sudo "$SCRIPT_DIR/ap_mode.sh"
fi

# Flask WiFi 설정 앱 기동 (app/ 기준)
export FLASK_APP="$BASE_DIR/app/wifi_config_app.py"
export FLASK_ENV=production
cd "$BASE_DIR"

# 0.0.0.0으로 listen 해야 AP에 접속한 클라이언트가 접근 가능
exec python3 -m flask run --host=0.0.0.0 --port=5000
