#!/usr/bin/env bash
# WiFi 연결 여부 확인
# 종료 코드: 0 = 연결됨, 1 = 미연결(SSID 없음 또는 인터넷 불가)
set -e

# wpa_supplicant에 설정된 네트워크가 있는지 확인 (ssid= 로 시작하는 줄)
WPA_CONF="${WPA_SUPPLICANT_CONF:-/etc/wpa_supplicant/wpa_supplicant.conf}"
if [ ! -f "$WPA_CONF" ]; then
  echo "[wifi-check] wpa_supplicant 설정 없음: $WPA_CONF"
  exit 1
fi

if ! grep -q '^\s*ssid=' "$WPA_CONF" 2>/dev/null; then
  echo "[wifi-check] 설정된 SSID 없음"
  exit 1
fi

# 실제 연결 확인: wlan0에 IP가 있고 외부 통신 가능한지
INTERFACE="${WIFI_INTERFACE:-wlan0}"
if ! ip -br addr show "$INTERFACE" 2>/dev/null | grep -q 'UP'; then
  echo "[wifi-check] $INTERFACE 비활성 또는 IP 없음"
  exit 1
fi

# 선택: ping으로 외부 연결 확인 (선택적, 라즈베리파이에서 인터넷 필요 시)
PING_HOST="${WIFI_PING_HOST:-8.8.8.8}"
if ! ping -c 1 -W 5 "$PING_HOST" &>/dev/null; then
  echo "[wifi-check] 외부 연결 실패 ($PING_HOST)"
  exit 1
fi

echo "[wifi-check] WiFi 연결됨"
exit 0
