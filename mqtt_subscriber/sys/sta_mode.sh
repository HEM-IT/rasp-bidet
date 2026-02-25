#!/usr/bin/env bash
# STA 모드 전환: AP(hostapd/dnsmasq) 중지 후 wpa_supplicant로 WiFi 클라이언트 모드
# root 또는 sudo로 실행 필요. 보통 SSID 등록 후 이 스크립트 호출 후 리부팅.
set -e

INTERFACE="${WIFI_INTERFACE:-wlan0}"
WPA_SUPPLICANT_CONF="/etc/wpa_supplicant/wpa_supplicant.conf"

echo "[sta_mode] STA 모드로 전환 중..."

# hostapd, dnsmasq 중지
sudo systemctl stop hostapd 2>/dev/null || true
sudo systemctl stop dnsmasq 2>/dev/null || true
sudo killall hostapd 2>/dev/null || true
sudo killall dnsmasq 2>/dev/null || true
sleep 1

# AP용 고정 IP 제거
sudo ip addr flush dev "$INTERFACE" 2>/dev/null || true

# wpa_supplicant 재기동 (systemd 사용 시)
if systemctl is-active --quiet wpa_supplicant 2>/dev/null; then
  sudo systemctl restart wpa_supplicant
else
  sudo wpa_supplicant -B -i "$INTERFACE" -c "$WPA_SUPPLICANT_CONF"
fi

# DHCP 클라이언트로 IP 획득 (dhcpcd 사용 가정)
if command -v dhcpcd &>/dev/null; then
  sudo dhcpcd "$INTERFACE" 2>/dev/null || true
elif command -v dhclient &>/dev/null; then
  sudo dhclient "$INTERFACE" 2>/dev/null || true
fi

echo "[sta_mode] STA 모드 전환 완료. 재부팅 권장: sudo reboot"
