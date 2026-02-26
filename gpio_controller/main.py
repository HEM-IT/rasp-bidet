# -*- coding: utf-8 -*-
"""
gpio_controller 진입점.
- MQTT command/start(measurement/start) 수신 시 1회 실행 (스위치 대체).
- 실측: 명령어 1회 수신 시, 레거시와 동일 처리 순서(ADC→filter→PPM append→smooth_peak→update_feces_st→종료 후 시프트·trapz)로 1회 실행. NoFeces(0) 1장 → 가스 루프 → Feces 1,2,3 촬영 → 슬롯 1,2,3만 저장·전송 → measurement API 1회.
- 시뮬레이션: 더미 가스 + 더미 이미지 분석 후 API 1회.

Sleep: time.sleep(1)은 실측 경로(else 블록)에만 있음(팬 안정화, 디스플레이, 0번 촬영 후 각 1초). 시뮬레이션 경로에는 sleep 없어 즉시 진행됨.
API payload: process_sensor_data가 gas_controller 반환값에서 스키마 필드(h2s_offset_ppm, time_sec, vocs_offset_ppm 등)를 그대로 복사. created_at은 전송 직전 main에서 설정. DB에 안 들어가면 API 서버 측 INSERT/매핑 확인 필요.
"""
import os
import sys
import json
import random
import time
import gc
import urllib.request
import urllib.error
import ssl
import re
from datetime import datetime

import config
from gas_controller import (
    measure_once as gas_measure_once,
    measure_once_simulation,
    measure_sequence,
    measure_sequence_simulation,
    fan_start,
)
from camera_controller import (
    capture_once as camera_capture_once,
    capture_at_slot,
    upload_captured_slots,
    upload_image_to_server,
    get_dummy_image_analysis,
    fetch_image_analysis_result,
    build_image_analysis_table_payload_for_api,
)
from utils import process_sensor_data, Camera_LED
from schema import MEASUREMENT_KEYS
try:
    from display_function import SSD1306_DISPLAY, Reset_Display
except ImportError:
    def _noop_display(*args, **kwargs):
        pass
    SSD1306_DISPLAY = Reset_Display = _noop_display
from device_status_api import (
    ensure_ready_then_set,
    update_device_status,
    STATUS_DETECTING,
    STATUS_MEASURING,
    STATUS_COMPLETED,
)


def merge_measurement_with_image_analysis(record, camera_data):
    """
    가스 측정 record에 이미지 분석 결과를 병합하여 최종 API payload 구성.
    camera_data에 upload_response(업로드 응답) 또는 image_analysis가 있으면 payload에 포함.
    :param record: process_sensor_data() 등으로 만든 측정 레코드 (dict)
    :param camera_data: camera_capture_once() 반환값 (upload_response, result_url 등)
    :return: record를 복사한 뒤 image_analysis 관련 필드를 추가한 dict (수정 없이 새 dict 반환)
    """
    out = dict(record)
    if not camera_data:
        return out
    # 업로드 응답에 분석 결과가 포함된 경우
    if isinstance(camera_data.get("upload_response"), dict):
        out["image_upload_response"] = camera_data["upload_response"]
    # 별도 이미지 분석 결과 키가 있으면 포함 (서버에서 조회 후 넣는 경우)
    if "image_analysis" in camera_data:
        out["image_analysis"] = camera_data["image_analysis"]
    if camera_data.get("result_url"):
        out["image_result_url"] = camera_data["result_url"]
    return out


def send_pending_json_from_folder(json_dir, api_base_url, remove_on_success=True, extra_headers=None):
    """
    json_file/ 폴더 내 JSON 파일을 measurement API로 일괄 전송 (레거시 주석 코드 반영).
    전송 성공(200/201) 시 해당 파일 삭제.
    :param json_dir: JSON 파일이 있는 디렉터리
    :param api_base_url: API 베이스 URL (DATA_API_URL)
    :param remove_on_success: 성공 시 파일 삭제 여부
    :param extra_headers: 추가 헤더 dict (예: HEM_HEADER)
    :return: list of (filename, status_code or None)
    """
    path = getattr(config, "DATA_API_MEASUREMENT_PATH", "/mqtt/api/v1/measurement")
    url = f"{api_base_url.rstrip('/')}{path}"
    if not os.path.isdir(json_dir):
        return []
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    results = []
    for fn in sorted(os.listdir(json_dir)):
        if not fn.lower().endswith(".json"):
            continue
        file_path = os.path.join(json_dir, fn)
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                body = f.read()
        except OSError:
            results.append((fn, None))
            continue
        req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST", headers=headers)
        ctx = ssl.create_default_context()
        if url.startswith("https://"):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                status = resp.status
                results.append((fn, status))
                if status in (200, 201) and remove_on_success:
                    os.remove(file_path)
        except urllib.error.HTTPError as e:
            results.append((fn, e.code))
        except Exception:
            results.append((fn, None))
    return results


def post_measurement(api_base_url, payload):
    path = getattr(config, "DATA_API_MEASUREMENT_PATH", "/mqtt/api/v1/measurement")
    url = f"{api_base_url.rstrip('/')}{path}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    ctx = ssl.create_default_context()
    if url.startswith("https://"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # 404 발생 지점: 위 url 로 POST 했을 때 서버가 404 반환 (경로/호스트 확인)
        print(f"[gpio_controller] API 404 요청 URL: {url}", file=sys.stderr)
        return e.code, None
    except Exception as e:
        raise RuntimeError(f"API 요청 실패: {e}") from e


def post_image_analysis(api_base_url, payload):
    """이미지 분석 결과를 /mqtt/api/v1/image_analysis API로 POST (image_analysis_table 스키마 포맷)."""
    path = getattr(config, "DATA_API_IMAGE_ANALYSIS_PATH", "/mqtt/api/v1/image_analysis")
    url = f"{api_base_url.rstrip('/')}{path}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    ctx = ssl.create_default_context()
    if url.startswith("https://"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"[gpio_controller] image_analysis API 오류 URL: {url} status: {e.code}", file=sys.stderr)
        return e.code, None
    except Exception as e:
        print(f"[gpio_controller] image_analysis API 요청 실패: {e}", file=sys.stderr)
        return None, None


def normalize_gas_id(device_id):
    """알파벳 5자리. 부족하면 채우고, 초과하면 자른다."""
    s = (device_id or "FFFFF").strip().upper()
    s = re.sub(r"[^A-Z]", "", s)[:5]
    return (s + "FFFFF")[:5]


def normalize_test_id(value):
    """5자리 숫자 00000~99999. 전달값 없으면 None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = int(value)
        if 0 <= v <= 99999:
            return str(v).zfill(5)
        return str(v)[-5:].zfill(5) if v > 0 else "00000"
    s = str(value).strip()
    if not s:
        return None
    digits = "".join(c for c in s if c.isdigit())
    if not digits:
        return None
    return digits[-5:].zfill(5) if len(digits) >= 5 else digits.zfill(5)


def generate_random_sensor_record():
    """시뮬레이션용: mqtt_sensor 에 넣을 랜덤 더미 센서 데이터 1건 (profile_id, gas_id, test_id 제외)."""
    return {
        "gas_version": "0.0.1",
        "h2s_abs_exposure": round(random.uniform(0, 10), 4),
        "h2s_offset_ppm": round(random.uniform(0, 5), 4),
        "h2s_ppm": round(random.uniform(0, 20), 4),
        "h2s_ratio_value_pct": round(random.uniform(0, 100), 2),
        "sort": random.randint(0, 99),
        "success": random.choice(["ok", "success"]),
        "time_sec": round(random.uniform(0, 300), 2),
        "total_abs_exposure": round(random.uniform(0, 50), 4),
        "vocs_abs_exposure": round(random.uniform(0, 10), 4),
        "vocs_offset_ppm": round(random.uniform(0, 5), 4),
        "vocs_ppm": round(random.uniform(0, 50), 4),
        "vocs_ratio_value_pct": round(random.uniform(0, 100), 2),
    }


def main():
    device_id = os.environ.get("DEVICE_ID", "FFFFF")
    payload_str = os.environ.get("MQTT_PAYLOAD", "{}")
    try:
        mqtt_payload = json.loads(payload_str)
    except json.JSONDecodeError:
        mqtt_payload = {}

    # 시뮬레이션 모드: 환경변수 GPIO_SIMULATION 또는 payload 의 simulation/test 플래그
    use_simulation = os.environ.get("GPIO_SIMULATION", "").lower() in ("1", "true", "yes")
    if not use_simulation and mqtt_payload:
        sim_flag = mqtt_payload.get("simulation") or mqtt_payload.get("test")
        use_simulation = sim_flag in (True, 1, "1", "true", "yes")

    # 명령에서 전달받은 값 (테스트 시 publisher 하드코딩 또는 body 로 전달)
    profile_id = mqtt_payload.get("profile_id")
    if profile_id is not None:
        profile_id = int(profile_id) if not isinstance(profile_id, int) else profile_id
    gas_id_from_payload = mqtt_payload.get("gas_id")
    gas_id = normalize_gas_id(gas_id_from_payload or device_id)
    test_id = normalize_test_id(mqtt_payload.get("test_id"))
    if test_id is None:
        raise ValueError("test_id is required in MQTT_PAYLOAD (5-digit numeric string)")

    api_base = config.DATA_API_URL
    print(f"[SCENARIO] 6. gpio_controller: PWM fan → gas_controller + camera_controller (레거시 순서) | simulation={use_simulation}", file=sys.stderr)
    # 디바이스 상태: ready → detecting → measuring → completed (8번: 진행 시마다 갱신)
    if api_base:
        try:
            ensure_ready_then_set(api_base, gas_id, STATUS_DETECTING)
            print("[SCENARIO] 7. Device status 갱신: detecting", file=sys.stderr)
        except Exception as e:
            print(f"[gpio_controller] device status ready/detecting 전송 실패: {e}", file=sys.stderr)

    if use_simulation:
        # 시뮬레이션: 시계열 더미 가스 + 더미 이미지 분석 (4장 촬영/업로드 생략). 여기에는 time.sleep(1) 없음 → 즉시 진행.
        print("[gpio_controller] 시뮬레이션 모드: 더미 가스 + 더미 이미지 분석", file=sys.stderr)
        if api_base:
            try:
                update_device_status(api_base, gas_id, STATUS_MEASURING)
                print("[SCENARIO] 8. Device status 갱신: measuring", file=sys.stderr)
            except Exception as e:
                print(f"[gpio_controller] device status measuring 전송 실패: {e}", file=sys.stderr)
        gas_data = measure_sequence_simulation()
        print("[gpio_controller] gas_controller(시뮬) 완료", file=sys.stderr)
        record = process_sensor_data(gas_data, None)
        record["profile_id"] = profile_id
        record["gas_id"] = gas_id
        record["test_id"] = test_id
        print("[gpio_controller] camera_capture_once 호출 (시뮬 1회)", file=sys.stderr)
        camera_data = camera_capture_once(gas_id=gas_id, test_id=test_id, simulation=True)
        print("[gpio_controller] camera_controller(시뮬) 데이터 취합 완료", file=sys.stderr)
        record = merge_measurement_with_image_analysis(record, camera_data)
        # 시뮬: image_analysis_table 스키마 포맷 더미를 /mqtt/api/v1/image_analysis 로 전송 (7번: camera 데이터 API, 1회만 호출)
        if api_base:
            try:
                print("[gpio_controller] post_image_analysis 호출 (시뮬 1회)", file=sys.stderr)
                ia_payload = build_image_analysis_table_payload_for_api(gas_id, test_id)
                status_ia, _ = post_image_analysis(api_base, ia_payload)
                if status_ia in (200, 201):
                    print("[SCENARIO] 7. image_analysis API 전송 완료 (camera 데이터)", file=sys.stderr)
            except Exception as e:
                print(f"[gpio_controller] image_analysis API 전송 실패: {e}", file=sys.stderr)
    
    # 여기서부터 실제 구동(프로덕션 모드)
    else:
        data_file_name = f"{gas_id}{test_id}"
        cwd = getattr(config, "GPIO_CONTROLLER_DIR", os.path.dirname(os.path.abspath(__file__)))

        
        pwm = fan_start()
        time.sleep(1)  # 팬 안정화 대기 (실측 경로에서만 동작; 시뮬 경로에는 sleep 없음)

        try:
            SSD1306_DISPLAY(gas_id, test_id)
        except Exception as e:
            print(f"[gpio_controller] SSD1306_DISPLAY 오류(무시): {e}", file=sys.stderr)
        time.sleep(1)  # 디스플레이 갱신 대기

        file_done = mqtt_payload.get("file_done", False) in (True, 1, "1", "true", "yes")
        Camera_LED("OFF" if file_done else "ON")

        image_time_0 = datetime.now().strftime("%Y%m%d%H%M%S")
        capture_at_slot(data_file_name, image_time_0, 0, cwd=cwd)
        image_times = [image_time_0]

        time.sleep(3)  # 0번 슬롯 촬영 후 대기 (libcamera 정리 등)
        gc.collect()

        
        if api_base:
            try:
                update_device_status(api_base, gas_id, STATUS_MEASURING)
                print("[SCENARIO] 8. Device status 갱신: measuring", file=sys.stderr)
            except Exception as e:
                print(f"[gpio_controller] device status measuring 전송 실패: {e}", file=sys.stderr)
        def _on_capture(slot, d, t):
            capture_at_slot(d, t, slot, cwd=cwd)
            image_times.append(t)

        gas_data = measure_sequence(
            gas_id, test_id, capture_callback=_on_capture, simulation=False, pwm=pwm
        )
        print("[gpio_controller] gas_controller(실측) 완료", file=sys.stderr)

        # 7) 루프 종료 후 Reset_Display (ref MainCode 214-216행)
        try:
            Reset_Display()
        except Exception as e:
            print(f"[gpio_controller] Reset_Display 오류(무시): {e}", file=sys.stderr)

        # 2) 슬롯 1,2,3을 Hong 서버(config.IMAGE_UPLOAD_URL)로 업로드 — camera_controller.upload_image_to_server 사용
        upload_results = []
        base = cwd or getattr(config, "GPIO_CONTROLLER_DIR", os.path.dirname(os.path.abspath(__file__)))
        for slot in (1, 2, 3):
            if slot >= len(image_times):
                upload_results.append((slot, False, None, "image_times 미존재"))
                continue
            image_time_str = image_times[slot]
            filename = f"{data_file_name}-{image_time_str}-{slot}.jpg"
            path = os.path.join(base, filename)
            if not os.path.isfile(path):
                upload_results.append((slot, False, filename, "파일 없음"))
                continue
            ok, resp = upload_image_to_server(path, filename)
            upload_results.append((slot, ok, filename, resp))
        last_upload_ok = None
        for _slot, ok, _fn, resp in upload_results:
            if ok and isinstance(resp, dict):
                last_upload_ok = resp
        analysis = fetch_image_analysis_result(gas_id, test_id)
        camera_data = {
            "upload_response": last_upload_ok,
            "image_analysis": analysis,
            "result_url": getattr(config, "IMAGE_ANALYSIS_RESULT_BASE", "image-analysis")
            + f"/{gas_id}/upload/{test_id}",
        }
        if not analysis and isinstance(last_upload_ok, dict) and "raw_bristol_type" in last_upload_ok:
            camera_data["image_analysis"] = last_upload_ok

        print("[gpio_controller] camera_controller(실측) 데이터 취합 완료", file=sys.stderr)
        
        record = process_sensor_data(gas_data, camera_data)
        record["profile_id"] = profile_id
        record["gas_id"] = gas_id
        record["test_id"] = test_id
        record = merge_measurement_with_image_analysis(record, camera_data)

    if not api_base:
        print("[gpio_controller] DATA_API_URL 없음, API 전송 생략", file=sys.stderr)
        return 0

    # DB 저장용 측정 완료 시각 (API에서 created_at 미수신 시 사용)
    record["created_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    # payload에 h2s_offset_ppm, time_sec, vocs_offset_ppm, created_at 포함 — API/DB에서 이 필드들을 저장하는지 확인 필요
    if os.environ.get("GPIO_DEBUG"):
        print(f"[gpio_controller] measurement payload 키: {list(record.keys())}", file=sys.stderr)
        print(f"[gpio_controller] h2s_offset_ppm={record.get('h2s_offset_ppm')} time_sec={record.get('time_sec')} vocs_offset_ppm={record.get('vocs_offset_ppm')} created_at={record.get('created_at')}", file=sys.stderr)

    try:
        print("[SCENARIO] 7. measurement API 전송 (gas 데이터)", file=sys.stderr)
        status, result = post_measurement(api_base, record)
        if status in (200, 201):
            if api_base:
                try:
                    update_device_status(api_base, gas_id, STATUS_COMPLETED)
                    print("[SCENARIO] 8. Device status 갱신: completed", file=sys.stderr)
                except Exception as e:
                    print(f"[gpio_controller] device status completed 전송 실패: {e}", file=sys.stderr)
            print(f"[gpio_controller] measurement API 전송 완료 status={status}", file=sys.stderr)
            return 0
        print(f"[gpio_controller] API 오류 status={status} result={result}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[gpio_controller] API 전송 실패: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[gpio_controller] 오류: {e}", file=sys.stderr)
        sys.exit(1)
