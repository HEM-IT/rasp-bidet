# -*- coding: utf-8 -*-
"""
Flask 기반 WiFi 설정 웹 앱.
AP 모드에서 사용자가 SSID/비밀번호 입력 → 검증(스캔 목록 기반) → 등록 후 STA 전환 및 재부팅.
"""
import os
import subprocess
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory

# app/ 폴더 기준; 스캔 캐시·sta_mode.sh 는 sys/ 에 있음
SCRIPT_DIR = Path(__file__).resolve().parent
SYS_DIR = (SCRIPT_DIR / ".." / "sys").resolve()
WPA_SUPPLICANT_CONF = os.environ.get("WPA_SUPPLICANT_CONF", "/etc/wpa_supplicant/wpa_supplicant.conf")
WIFI_SCAN_CACHE = SYS_DIR / ".wifi_scan_cache"
STA_SCRIPT = SYS_DIR / "sta_mode.sh"

app = Flask(__name__)


def get_scan_ssids():
    """AP 전환 전에 저장해 둔 스캔 캐시에서 SSID 목록 반환."""
    if not WIFI_SCAN_CACHE.exists():
        return []
    try:
        return [s.strip() for s in WIFI_SCAN_CACHE.read_text(encoding="utf-8").splitlines() if s.strip()]
    except Exception:
        return []


@app.route("/")
def index():
    return send_from_directory(SCRIPT_DIR / "templates", "wifi_config.html")


@app.route("/api/scan", methods=["GET"])
def api_scan():
    """스캔된 SSID 목록 반환 (웹에서 검증용)."""
    ssids = get_scan_ssids()
    return jsonify({"ssids": ssids})


@app.route("/api/validate", methods=["POST"])
def api_validate():
    """SSID가 스캔 목록에 있는지 검증."""
    data = request.get_json() or {}
    ssid = (data.get("ssid") or "").strip()
    if not ssid:
        return jsonify({"ok": False, "error": "SSID를 입력하세요."}), 400
    scan_list = get_scan_ssids()
    if scan_list and ssid not in scan_list:
        return jsonify({"ok": False, "error": "해당 SSID가 스캔 목록에 없습니다. 목록에서 선택하거나 주변에서 접속 가능한지 확인하세요."}), 400
    return jsonify({"ok": True})


def escape_ssid_for_wpa(ssid: str) -> str:
    """SSID에 따옴표/백슬래시가 있으면 이스케이프."""
    return ssid.replace("\\", "\\\\").replace('"', '\\"')


def escape_psk_for_wpa(psk: str) -> str:
    """PSK(비밀번호) 내 따옴표·백슬래시 이스케이프."""
    return psk.replace("\\", "\\\\").replace('"', '\\"')


@app.route("/api/register", methods=["POST"])
def api_register():
    """검증된 SSID/비밀번호를 wpa_supplicant에 추가 후 STA 전환 및 재부팅."""
    data = request.get_json() or {}
    ssid = (data.get("ssid") or "").strip()
    password = (data.get("password") or "").strip()

    if not ssid:
        return jsonify({"ok": False, "error": "SSID를 입력하세요."}), 400

    # 선택적 검증: 스캔 목록에 있으면 통과, 없어도 등록은 허용(수동 입력)
    scan_list = get_scan_ssids()
    if scan_list and ssid not in scan_list:
        return jsonify({"ok": False, "error": "SSID 검증 실패. 스캔 목록에 없습니다."}), 400

    try:
        # wpa_supplicant 설정에 network 블록 추가 (root 필요)
        block = (
            "\nnetwork={\n"
            '    ssid="' + escape_ssid_for_wpa(ssid) + '"\n'
            '    psk="' + escape_psk_for_wpa(password) + '"\n'
            "}\n"
        )
        # 기존 파일 백업 후 추가
        if os.path.exists(WPA_SUPPLICANT_CONF):
            with open(WPA_SUPPLICANT_CONF, "r", encoding="utf-8") as f:
                content = f.read()
        else:
            content = "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\nupdate_config=1\ncountry=KR\n"

        if "ssid=" in content and ssid in content:
            return jsonify({"ok": False, "error": "이미 등록된 SSID입니다."}), 400

        with open(WPA_SUPPLICANT_CONF, "w", encoding="utf-8") as f:
            f.write(content.rstrip() + block)

        # STA 모드 전환 후 재부팅 (이미 root면 sudo 불필요)
        run_cmd = ["sudo", str(STA_SCRIPT)] if os.geteuid() != 0 else [str(STA_SCRIPT)]
        subprocess.run(run_cmd, check=False, timeout=30)
        reboot_cmd = ["sudo", "systemctl", "reboot"] if os.geteuid() != 0 else ["systemctl", "reboot"]
        subprocess.run(reboot_cmd, check=False, timeout=5)
    except PermissionError:
        return jsonify({"ok": False, "error": "권한 없음. sudo로 실행하세요."}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True, "message": "등록 완료. 곧 재부팅됩니다."})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
