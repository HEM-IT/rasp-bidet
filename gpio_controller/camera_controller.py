# -*- coding: utf-8 -*-
"""
카메라 측정 컨트롤러.
- 실측: 4시점 촬영(슬롯 0 NoFeces, 1·2·3 Feces) 후 슬롯 1·2·3만 업로드. 이미지 분석 결과 수신.
- 시뮬레이션: 촬영/업로드 없이 4장(0~3) 분 더미 파일명·분석 데이터 반환.
- 촬영된 이미지는 config.IMAGE_UPLOAD_URL 로 multipart POST 전달 (ref/servlet.py 규격).
- 이미지 분석 결과: servlet 업로드 응답 + bristol_predict 형식(raw_bristol_type, color_type, color_rgb 등).

촬영 시점:
- 0번: 측정 시작 직후 1장 (main.py에서 1회 호출). 업로드 제외.
- 1~3번: 연속 촬영이 아님. gas_controller 측정 루프에서 idx == feces_st + (30, 60, 120) 일 때
  각각 1장씩 촬영. 30/60/120 오프셋은 gas_controller.CAPTURE_IDX_OFFSETS 또는 환경변수 CAPTURE_IDX 로 변경 가능.

레거시 동작 (docs/LEGACY_ANALYSIS.md 참고):
- 연결 확인: libcamera-still -t 2000 -o test.jpg
- 4시점 촬영: NoFeces(0), Feces 1/2/3 → data_file_name-image_time-0~3.jpg
"""
import os
import sys
import json
import random
import shutil
from datetime import datetime
import urllib.request
import urllib.error
import config

# 서버 규격: filename 에서 gas_id(5자), test_id, 촬영시각 파싱 (ref/servlet.py)
# filename 형식: {gas_id}{test_id}-{YYYYmmddHHMMSS}-.jpg (마지막 '-'로 split 시 image_info[1]에 확장자 안 붙음 → servlet strptime 500 회피)
# 예: FFFFF00042-20250213120500-.jpg

# 레거시: 촬영 타임아웃(ms), 자동초점 옵션
LIBCAMERA_STILL_TIMEOUT_MS = int(os.environ.get("LIBCAMERA_STILL_TIMEOUT_MS", "2000"))
LIBCAMERA_AUTOFOCUS = os.environ.get("LIBCAMERA_AUTOFOCUS", "1").lower() in ("1", "true", "yes")


def check_camera_connection(timeout_sec=2, retries=2):
    """
    libcamera-still로 카메라 연결 여부 확인 (Raspberry Pi).
    :param timeout_sec: 촬영 대기 시간(초)
    :param retries: 실패 시 재시도 횟수
    :return: bool - 연결 성공 여부
    """
    for _ in range(max(1, retries)):
        try:
            t_ms = min(10000, max(1000, int(timeout_sec * 1000)))
            out = os.path.join(config.GPIO_CONTROLLER_DIR, "tmp", "test.jpg")
            os.makedirs(os.path.dirname(out), exist_ok=True)
            cmd = f"libcamera-still -t {t_ms} -o {out}"
            if os.system(cmd) == 0 and os.path.isfile(out):
                return True
        except Exception:
            pass
    return False


def capture_to_file(save_path, timeout_ms=None, autofocus_on_capture=True):
    """
    libcamera-still로 1회 촬영하여 save_path에 저장 (Raspberry Pi).
    :param save_path: 저장 경로 (예: /home/pi/FFFFF00042-20250216120000-0.jpg)
    :param timeout_ms: 촬영 대기 ms (기본 LIBCAMERA_STILL_TIMEOUT_MS)
    :param autofocus_on_capture: --autofocus-on-capture 사용 여부
    :return: bool - 성공 여부
    """
    timeout_ms = timeout_ms or LIBCAMERA_STILL_TIMEOUT_MS
    os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
    af = " --autofocus-on-capture" if autofocus_on_capture else ""
    cmd = f"libcamera-still -t {timeout_ms}{af} -o {save_path}"
    if os.system(cmd) == 0:
        return os.path.isfile(save_path)
    return False


def capture_at_slot(data_file_name, image_time_str, slot_index, cwd=None):
    """
    레거시 4시점 촬영: data_file_name-image_time-0|1|2|3.jpg
    :param data_file_name: gas_id + test_id (예: FFFFF00042)
    :param image_time_str: 해당 슬롯의 촬영 시각 문자열 (YYYYmmddHHMMSS)
    :param slot_index: 0(NoFeces), 1,2,3(Feces)
    :param cwd: 촬영 시 작업 디렉터리 (기본 GPIO_CONTROLLER_DIR 또는 /tmp)
    :return: (success: bool, path: str)
    """
    base = cwd or config.GPIO_CONTROLLER_DIR
    save_path = os.path.join(base, f"{data_file_name}-{image_time_str}-{slot_index}.jpg")
    ok = capture_to_file(save_path)
    return ok, save_path


def upload_captured_slots(data_file_name, image_times, slots_to_upload=None, cwd=None):
    """
    레거시: 4장 중 첫 장(0)은 저장/전송하지 않고, 슬롯 1,2,3만 업로드.
    :param data_file_name: gas_id + test_id (예: FFFFF00042)
    :param image_times: [time0, time1, time2, time3] 문자열 리스트 (최소 슬롯 인덱스+1 길이)
    :param slots_to_upload: 업로드할 슬롯 (기본 (1, 2, 3))
    :param cwd: 이미지가 저장된 디렉터리 (기본 config.GPIO_CONTROLLER_DIR)
    :return: list of (slot_index, success: bool, filename, response_or_error)
    """
    if slots_to_upload is None:
        slots_to_upload = (1, 2, 3)
    base = cwd or config.GPIO_CONTROLLER_DIR
    results = []
    for slot in slots_to_upload:
        if slot >= len(image_times):
            results.append((slot, False, None, "image_times 미존재"))
            continue
        image_time_str = image_times[slot]
        filename = f"{data_file_name}-{image_time_str}-{slot}.jpg"
        path = os.path.join(base, filename)
        if not os.path.isfile(path):
            results.append((slot, False, filename, "파일 없음"))
            continue
        ok, resp = _upload_image_to_server(path, filename)
        results.append((slot, ok, filename, resp))
    return results


def move_images_to_image_folder(data_file_name, image_times, source_dirs, image_file_dir):
    """
    촬영된 4장을 image_file 폴더로 이동/복사.
    :param data_file_name: gas_id+test_id
    :param image_times: [time0, time1, time2, time3] 문자열 리스트
    :param source_dirs: 찾을 디렉터리 목록 (예: ['/home/pi', ADCPi_path])
    :param image_file_dir: 목적지 image_file/ 경로
    :return: list of (slot_index, dest_path) 이동된 파일
    """
    os.makedirs(image_file_dir, exist_ok=True)
    moved = []
    for i in range(min(4, len(image_times))):
        name = f"{data_file_name}-{image_times[i]}-{i}.jpg"
        dest = os.path.join(image_file_dir, name)
        for d in source_dirs:
            src = os.path.join(d, name)
            if os.path.isfile(src):
                try:
                    shutil.copy2(src, dest)
                    moved.append((i, dest))
                except Exception:
                    pass
                break
    return moved


def send_pending_images_from_folder(image_file_dir=None, remove_on_success=True):
    """
    image_file/ 폴더 내 이미지를 config.IMAGE_UPLOAD_URL 로 일괄 전송.
    200 응답 시 해당 파일 삭제 (remove_on_success=True).
    :param image_file_dir: image_file 폴더 경로 (기본: config.GPIO_CONTROLLER_DIR/tmp/image_file)
    :param remove_on_success: 전송 성공 시 로컬 파일 삭제 여부
    :return: list of (filename, success: bool)
    """
    if image_file_dir is None:
        image_file_dir = os.path.join(config.GPIO_CONTROLLER_DIR, "tmp", "image_file")
    if not os.path.isdir(image_file_dir):
        return []
    results = []
    for fn in sorted(os.listdir(image_file_dir)):
        if not fn.lower().endswith(".jpg"):
            continue
        path = os.path.join(image_file_dir, fn)
        ok, _ = _upload_image_to_server(path, fn)
        results.append((fn, ok))
        if ok and remove_on_success:
            try:
                os.remove(path)
            except OSError:
                pass
    return results


def _capture_image_to_file(save_path):
    """
    실제 카메라 1회 촬영하여 save_path에 저장.
    라즈베리파이에서 libcamera 사용 가능 시 capture_to_file() 호출, 아니면 더미.
    :param save_path: 저장할 이미지 파일 경로 (예: /tmp/hem_capture_xxxx.jpg)
    :return: bool - 성공 여부
    """
    if LIBCAMERA_AUTOFOCUS:
        if capture_to_file(save_path):
            return True
    # fallback: 더미 (개발 환경 등)
    if not os.path.exists(save_path):
        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        open(save_path, "wb").close()
    return os.path.exists(save_path)


# 이미지 분석 API/테이블용 상수: Bristol B1~B7, Color C1~C17
BRISTOL_TYPES = ["B1", "B2", "B3", "B4", "B5", "B6", "B7"]
COLOR_TYPES = [f"C{i}" for i in range(1, 18)]


# 시뮬레이션 모드에 해당하는 함수
def build_image_analysis_table_payload_for_api(gas_id, test_id):
    """
    /mqtt/api/v1/image_analysis API 및 image_analysis_table 스키마에 맞는 더미 payload 생성.
    - 4장(슬롯 0~3) 모두 채움: file_name_0~3, data_captured_time_0~3, raw_bristol_type_0~3, raw_color_type_0~3.
    - gas_id, test_id: sim 모드와 상관없이 정상 전달 (5자리 유지).
    - raw_bristol_type_0~3, bristol_type: B1~B7 중 랜덤.
    - raw_color_type_0~3, color_type: C1~C17 중 랜덤.
    """
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    gas_id = (gas_id or "FFFFF").strip().upper()[:5].ljust(5, "F")
    test_id = (test_id or "00000").strip()
    digits = "".join(c for c in test_id if c.isdigit())
    test_id = digits.zfill(5)[-5:] if digits else "00000"

    bristol_0 = random.choice(BRISTOL_TYPES)
    bristol_1 = random.choice(BRISTOL_TYPES)
    bristol_2 = random.choice(BRISTOL_TYPES)
    bristol_3 = random.choice(BRISTOL_TYPES)
    color_0 = random.choice(COLOR_TYPES)
    color_1 = random.choice(COLOR_TYPES)
    color_2 = random.choice(COLOR_TYPES)
    color_3 = random.choice(COLOR_TYPES)
    r, g, b = [random.randint(50, 200) for _ in range(3)]
    rgb_color = f"{r},{g},{b}"

    ts_file = now.strftime("%Y%m%d%H%M%S")
    return {
        "image_version": "GV.1.0",
        "gas_id": gas_id,
        "test_id": test_id,
        "file_name_0": f"{gas_id}{test_id}-{ts_file}-0.jpg",
        "file_name_1": f"{gas_id}{test_id}-{ts_file}-1.jpg",
        "file_name_2": f"{gas_id}{test_id}-{ts_file}-2.jpg",
        "file_name_3": f"{gas_id}{test_id}-{ts_file}-3.jpg",
        "data_captured_time_0": now_str,
        "data_captured_time_1": now_str,
        "data_captured_time_2": now_str,
        "data_captured_time_3": now_str,
        "input_datetime": now_str,
        "output_datetime": now_str,
        "raw_bristol_type_0": bristol_0,
        "raw_bristol_type_1": bristol_1,
        "raw_bristol_type_2": bristol_2,
        "raw_bristol_type_3": bristol_3,
        "bristol_type": bristol_0,
        "raw_color_type_0": color_0,
        "raw_color_type_1": color_1,
        "raw_color_type_2": color_2,
        "raw_color_type_3": color_3,
        "color_type": color_0,
        "rgb_color": rgb_color,
        "time_duration": round(random.uniform(0.2, 1.5), 3),
        "process_success": 1,
    }


def get_dummy_image_analysis(gas_id, test_id, filename=None):
    """
    시뮬레이션용 이미지 분석 더미 (bristol_predict.py / function_for_bristol.py / servlet.py 형식).
    - raw_bristol_type: 1~7 (Bristol scale) 또는 -1 (no stool)
    - color_type: 1~17 (stool_color_type_hsv)
    - color_rgb: [R, G, B]
    - bristol_proba: 7개 클래스 확률 (선택)
    """
    if filename is None:
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        filename = f"{gas_id}{test_id}-{ts}-.jpg"
    # Bristol 1~7 중 하나 또는 -1
    raw_bristol_type = random.choice([1, 2, 3, 4, 5, 6, 7, -1])
    # color_type 1~17 (function_for_bristol closest_type_hsv)
    color_type = random.randint(1, 17) if raw_bristol_type != -1 else -1
    # color_rgb [R,G,B] 0~255
    color_rgb = [random.randint(50, 200) for _ in range(3)] if raw_bristol_type != -1 else [0, 0, 0]
    # 7-class proba (bristol_1~7)
    proba = [random.random() for _ in range(7)]
    total = sum(proba)
    bristol_proba = [round(p / total, 4) for p in proba]
    return {
        "files_processed": [filename],
        "raw_bristol_type": raw_bristol_type,
        "bristol_proba": bristol_proba,
        "img_name": filename,
        "color_type": color_type,
        "color_rgb": color_rgb,
        "color_dur": round(random.uniform(0.1, 1.0), 3),
        "predict_dur": round(random.uniform(0.5, 2.0), 3),
    }


def fetch_image_analysis_result(gas_id, test_id, timeout_sec=30):
    """
    이미지 분석 결과 API에서 Bristol/색상 결과 조회 (GET).
    config.IMAGE_ANALYSIS_RESULT_URL 이 비어 있으면 None 반환.
    반환 형식: get_dummy_image_analysis() 와 동일한 키 (raw_bristol_type, color_type, color_rgb 등).
    """
    base = getattr(config, "IMAGE_ANALYSIS_RESULT_URL", "").strip()
    if not base:
        return None
    url = f"{base}/{gas_id}/upload/{test_id}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            data = json.loads(resp.read().decode())
            return data if isinstance(data, dict) else None
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, OSError):
        return None
# 여기까지 시뮬레이션 모드에 해당하는 함수

def _upload_image_to_server(file_path, filename):
    """
    촬영된 이미지 파일을 config.IMAGE_UPLOAD_URL 로 multipart POST 전송.
    ref/servlet.py: 'file' 키로 파일 수신, filename으로 gas_id/test_id/촬영시각 파싱.
    :param file_path: 로컬 파일 경로
    :param filename: 서버에 보낼 파일명 (gas_id+test_id-YYYYmmddHHMMSS-.jpg, servlet strptime 호환)
    :return: (success: bool, response_data or error_message)
    """
    url = config.IMAGE_UPLOAD_URL.rstrip("/")
    if not url:
        return False, "IMAGE_UPLOAD_URL 미설정"

    boundary = "----WebKitFormBoundary" + os.urandom(16).hex()
    body_start = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: image/jpeg\r\n\r\n"
    )
    body_end = f"\r\n--{boundary}--\r\n"

    try:
        with open(file_path, "rb") as f:
            file_data = f.read()
    except OSError as e:
        return False, str(e)

    body = body_start.encode("utf-8") + file_data + body_end.encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return True, json.loads(resp.read().decode())
    except Exception as e:
        return False, str(e)


# Hong 서버(IMAGE_UPLOAD_URL) 업로드용 공개 API — main.py 등에서 직접 호출
upload_image_to_server = _upload_image_to_server


def capture_once(gas_id, test_id, simulation=False):
    """
    카메라 촬영 후 이미지 전송, 필요 시 이미지 분석 결과까지 수신.
    - simulation=True: 촬영/업로드 없이 4장(슬롯 0~3) 분 더미 파일명·Bristol·색상 분석 반환 (servlet/bristol_predict 형식).
    - simulation=False: 1회 촬영 → IMAGE_UPLOAD_URL 로 POST(servlet) → 분석 결과 조회(IMAGE_ANALYSIS_RESULT_URL) 후 반환.
    :param gas_id: 5자리 gas_id (예: FFFFF)
    :param test_id: 5자리 test_id (예: 00042)
    :param simulation: True면 더미 이미지 분석만 반환
    :return: dict - image_path, uploaded, upload_response, result_url, image_analysis(분석 결과) 등
    """
    print(f"[camera_controller] capture_once 진입 gas_id={gas_id} test_id={test_id} simulation={simulation}", file=sys.stderr)
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    filename = f"{gas_id}{test_id}-{ts}-.jpg"
    tmp_dir = os.environ.get("HEM_CAPTURE_DIR", os.path.join(config.GPIO_CONTROLLER_DIR, "tmp"))
    os.makedirs(tmp_dir, exist_ok=True)
    save_path = os.path.join(tmp_dir, filename)
    result_base = getattr(config, "IMAGE_ANALYSIS_RESULT_BASE", "image-analysis")
    result_url_path = f"{result_base}/{gas_id}/upload/{test_id}"

    out = {
        "image_path": save_path,
        "filename": filename,
        "analyzed": False,
        "uploaded": False,
        "result_url": result_url_path,
        "upload_response": None,
        "image_analysis": None,
    }

    if simulation:
        # 4장(슬롯 0~3) 더미: file_name 형식 {gas_id}{test_id}-{ts}-{0|1|2|3}.jpg
        files_processed = [f"{gas_id}{test_id}-{ts}-{i}.jpg" for i in range(4)]
        out["filename"] = files_processed[0]
        out["uploaded"] = True
        out["upload_response"] = {"files_processed": files_processed}
        out["image_analysis"] = get_dummy_image_analysis(gas_id, test_id, files_processed[0])
        out["analyzed"] = True
        print("[camera_controller] 시뮬레이션: 4장 더미 이미지 분석 반환", file=sys.stderr)
        return out

    if not _capture_image_to_file(save_path):
        print("[camera_controller] 촬영 실패 (의사코드 구현 필요)", file=sys.stderr)
        return out

    ok, resp = _upload_image_to_server(save_path, filename)
    out["uploaded"] = ok
    out["upload_response"] = resp if ok else {"error": resp}
    if ok:
        print(f"[camera_controller] 이미지 전송 완료: {filename}", file=sys.stderr)
    else:
        print(f"[camera_controller] 이미지 전송 실패: {resp}", file=sys.stderr)
        return out

    # 업로드 성공 후 이미지 분석 결과 조회 (동일 서버 결과 API 또는 별도 URL)
    analysis = fetch_image_analysis_result(gas_id, test_id)
    if analysis:
        out["image_analysis"] = analysis
        out["analyzed"] = True
    elif isinstance(out.get("upload_response"), dict) and "raw_bristol_type" in out["upload_response"]:
        out["image_analysis"] = out["upload_response"]
        out["analyzed"] = True
    return out
