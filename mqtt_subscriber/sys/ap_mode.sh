#!/usr/bin/env bash
# AP 모드 전환: hostapd + dnsmasq 기동, 사용자가 라즈베리파이에 접속해 WiFi 설정 가능
# root 또는 sudo로 실행 필요
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INTERFACE="${WIFI_INTERFACE:-wlan0}"
AP_IP="${AP_IP:-192.168.4.1}"
AP_SSID="${AP_SSID:-HEM-WiFi-Setup}"
AP_PASSPHRASE="${AP_PASSPHRASE:-hem12345}"

HOSTAPD_CONF="/etc/hostapd/hostapd.conf"
DNSMASQ_CONF="/etc/dnsmasq.d/ap-wlan.conf"
WPA_SUPPLICANT_CONF="/etc/wpa_supplicant/wpa_supplicant.conf"

echo "[ap_mode] AP 모드 설정 중..."

# wpa_supplicant 중지 (STA로 사용 중이면)
sudo systemctl stop wpa_supplicant 2>/dev/null || true
sudo killall wpa_supplicant 2>/dev/null || true
sleep 1

# 기존 hostapd/dnsmasq 중지
sudo systemctl stop hostapd 2>/dev/null || true
sudo systemctl stop dnsmasq 2>/dev/null || true
sudo killall hostapd 2>/dev/null || true
sudo killall dnsmasq 2>/dev/null || true
sleep 1

# IP 플러시 후 AP용 고정 IP
sudo ip addr flush dev "$INTERFACE" 2>/dev/null || true
sudo ip addr add "${AP_IP}/24" dev "$INTERFACE"
sudo ip link set "$INTERFACE" up

# hostapd 설정
sudo mkdir -p "$(dirname "$HOSTAPD_CONF")"
sudo tee "$HOSTAPD_CONF" >/dev/null <<EOF
interface=${INTERFACE}
driver=nl80211
ssid=${AP_SSID}
hw_mode=g
channel=6
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=${AP_PASSPHRASE}
wpa_key_mgmt=WPA-PSK
wpa_pairwise=TKIP
rsn_pairwise=CCMP
EOF

# dnsmasq 설정 (AP 대역 DHCP)
sudo mkdir -p "$(dirname "$DNSMASQ_CONF")"
sudo tee "$DNSMASQ_CONF" >/dev/null <<EOF
interface=${INTERFACE}
bind-interfaces
dhcp-range=192.168.4.10,192.168.4.50,255.255.255.0,12h
dhcp-option=3,${AP_IP}
EOF

# 기존 dnsmasq 설정이 0.0.0.0 listen 하면 충돌할 수 있음; 여기서는 interface만 사용
sudo systemctl unmask hostapd 2>/dev/null || true
sudo systemctl start hostapd
sudo systemctl start dnsmasq

echo "[ap_mode] AP 모드 활성화 완료. SSID: ${AP_SSID}, IP: ${AP_IP}"
echo "[ap_mode] 사용자 접속: http://${AP_IP}:5000"
