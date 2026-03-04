# -*- coding: utf-8 -*-
"""
가스 센서 / GPIO 제어.
- simulation=False 일 때 실제 센서 1회 측정 후 DB 스키마와 동일한 필드로 dict 반환.
- gas_id, test_id 는 main.py에서 설정 (gas_controller는 측정값만 반환).

레거시 동작 (docs/LEGACY_ANALYSIS.md 참고):
- ADCPi 채널 1=H2S, 2=VOCs, 8=스위치(>3V 측정 시작).
- 1차 저역통과 필터 → PPM 변환 → feces_st 감지(noise_1/noise_5) → 종료 후 시프트·오프셋·trapz 적분·비율 계산 → JSON 생성.
- FAN PWM: FAN_PIN 12, 300Hz, duty_cycle 100%.

"""
import gc
import logging
import os
import sys
import time
from datetime import datetime
from collections import OrderedDict

# 모듈 로거 (단계별 log 호출용)
log = logging.getLogger(__name__)

try:
    from utils import filter as legacy_filter
except ImportError:
    legacy_filter = None

try:
    import config
except ImportError:
    config = None

try:
    from device_status_api import (
        ensure_ready_then_set,
        update_device_status,
        get_current_status,
        STATUS_DETECTING,
        STATUS_MEASURING,
        STATUS_FAIL,
        STATUS_STOP,
        STATUS_READY,
    )
except ImportError:
    ensure_ready_then_set = update_device_status = get_current_status = None
    STATUS_DETECTING = "detecting"
    STATUS_MEASURING = "measuring"
    STATUS_FAIL = "fail"
    STATUS_STOP = "stop"
    STATUS_READY = "ready"

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

# ----- 레거시 상수 -----
BM_TIME = int(os.environ.get("BM_TIME", "8"))           # baseline 구간 길이 (샘플 수)
END_TR = int(os.environ.get("END_TR", "180"))           # feces_st + end_tr 에서 측정 종료 (샘플 수, 1Hz 시 180초=3분)
SHORT_TR = int(os.environ.get("SHORT_TR", "135"))      # 10+2
LONG_TR = int(os.environ.get("LONG_TR", "20"))         # 5+2
# 루프 주기(초): 1.0 이면 1샘플/초 → 8샘플=8초 베이스라인, 180샘플=3분 측정. 0이면 sleep 없음(최대 속도).
MEASURE_LOOP_INTERVAL_SEC = float(os.environ.get("MEASURE_LOOP_INTERVAL_SEC", "1.0"))
FAN_PIN = int(os.environ.get("FAN_PIN", "12"))
FAN_FREQUENCY_HZ = int(os.environ.get("FAN_FREQUENCY_HZ", "300"))
FAN_DUTY_CYCLE_PCT = int(os.environ.get("FAN_DUTY_CYCLE_PCT", "100"))

# PPM 변환 계수 (원본 공식)
H2S_DIVISOR = 120 * 4500   # (H2S_filtered_v - 0.5) * 1e6 / H2S_DIVISOR
VOCS_DIVISOR = 35 * 1800   # (VOCs_filtered_v - 0.5) * 1e6 / VOCS_DIVISOR
VOLTAGE_OFFSET = 0.5

# feces_st 감지 임계값
NOISE_1_THRESHOLD = float(os.environ.get("NOISE_1_THRESHOLD", "0.006"))
NOISE_5_THRESHOLD = float(os.environ.get("NOISE_5_THRESHOLD", "0.01"))
NOISE_5_THRESHOLD_HIGH = float(os.environ.get("NOISE_5_THRESHOLD_HIGH", "0.015"))
STABLE_THRE = float(os.environ.get("STABLE_THRE", "0.004"))

# 슬롯 1,2,3 촬영 시점: idx == feces_st + CAPTURE_IDX_OFFSETS[i] 일 때 capture_callback(slot=i+1) 호출.
# 환경변수 CAPTURE_IDX: 쉼표 구분 "30,60,120" (기본값). 예: CAPTURE_IDX=30,60,120
_def_capture_idx = os.environ.get("CAPTURE_IDX", "30,60,120").strip()
CAPTURE_IDX_OFFSETS = tuple(int(x.strip()) for x in _def_capture_idx.split(",") if x.strip())[:3]
if len(CAPTURE_IDX_OFFSETS) < 3:
    CAPTURE_IDX_OFFSETS = (30, 60, 120)  # fallback

log.debug("gas_controller constants loaded: BM_TIME=%s, END_TR=%s, MEASURE_LOOP_INTERVAL_SEC=%s, CAPTURE_IDX_OFFSETS=%s",
          BM_TIME, END_TR, MEASURE_LOOP_INTERVAL_SEC, CAPTURE_IDX_OFFSETS)


def filter_voltage(voltage, b_prev, alpha=0.1):
    """
    1차 저역통과 필터 (지수이동평균).
    filtered = alpha * voltage + (1 - alpha) * b_prev
    :return: (filtered_v, b_new, a_new) — b_new는 다음 스텝의 b_prev로 사용
    """
    filtered = alpha * float(voltage) + (1.0 - alpha) * float(b_prev)
    log.debug("filter_voltage: voltage=%.4f b_prev=%.4f alpha=%.2f -> filtered=%.4f", voltage, b_prev, alpha, filtered)
    return filtered, filtered, alpha


def voltage_to_ppm_h2s(voltage):
    """H2S 전압 → PPM (원본 공식)."""
    v = float(voltage) - VOLTAGE_OFFSET
    ppm = (v * 1e6) / H2S_DIVISOR if H2S_DIVISOR else 0.0
    log.debug("voltage_to_ppm_h2s: voltage=%.4f -> ppm=%.4f", voltage, ppm)
    return ppm


def voltage_to_ppm_vocs(voltage):
    """VOCs 전압 → PPM (원본 공식)."""
    v = float(voltage) - VOLTAGE_OFFSET
    ppm = (v * 1e6) / VOCS_DIVISOR if VOCS_DIVISOR else 0.0
    log.debug("voltage_to_ppm_vocs: voltage=%.4f -> ppm=%.4f", voltage, ppm)
    return ppm


def smooth_peak_h2s(H2S_raw_ppm, idx):
    """
    H2S 피크 제거: idx가 극값이면서 양옆 차이 < STABLE_THRE 이면 idx를 이전 값으로 대체.
    :param H2S_raw_ppm: 리스트 (최소 idx+2 길이)
    :param idx: 중간 인덱스 (temp_stt)
    """
    if idx < 1 or idx + 1 >= len(H2S_raw_ppm):
        log.debug("smooth_peak_h2s: idx=%s out of range, skip", idx)
        return
    a, b, c = H2S_raw_ppm[idx - 1], H2S_raw_ppm[idx], H2S_raw_ppm[idx + 1]
    if (b - a) * (c - b) < 0 and abs(c - a) < STABLE_THRE:
        if abs(b - a) * abs(b - c) > 0.005 * 0.005:
            H2S_raw_ppm[idx] = a
            log.debug("smooth_peak_h2s: idx=%s peak removed (%.4f -> %.4f)", idx, b, a)


def update_feces_st(idx, H2S_raw_ppm, noise_1_list, noise_5_list, feces_st, BM_time=None):
    """
    feces_st 갱신: noise_1 / noise_5 임계값 초과 시 feces_st = idx-2 설정 (한 번만).
    :param idx: 현재 인덱스
    :param H2S_raw_ppm: H2S PPM 리스트
    :param noise_1_list: [noise_1 값들] (append 사용)
    :param noise_5_list: [noise_5 값들] (append 사용)
    :param feces_st: 현재 feces_st (0이면 미감지)
    :param BM_time: BM_time (기본 모듈 상수)
    :return: (feces_st_new, noise_1_list, noise_5_list)
    """
    # 호출 여부 확인용 (10회마다만 출력하여 로그 과다 방지)
    if idx == 2 or (idx > 0 and idx % 10 == 0):
        print(f"[gpio_controller] [update_feces_st] 호출 idx={idx} feces_st={feces_st} len(H2S_raw_ppm)={len(H2S_raw_ppm)}", file=sys.stderr)

    bm = BM_time if BM_time is not None else BM_TIME
    if feces_st != 0 or idx <= 1:
        if idx == 2 or (idx > 0 and idx % 10 == 0):
            print(f"[gpio_controller] [update_feces_st] 조기반환: feces_st!=0 or idx<=1 (idx={idx})", file=sys.stderr)
        return feces_st, noise_1_list, noise_5_list

    temp_stt = idx - 1
    if temp_stt < 2:
        if idx == 2 or (idx > 0 and idx % 10 == 0):
            print(f"[gpio_controller] [update_feces_st] 조기반환: temp_stt<2 (idx={idx})", file=sys.stderr)
        return feces_st, noise_1_list, noise_5_list

    # noise_1
    n1 = abs(H2S_raw_ppm[idx] - H2S_raw_ppm[idx - 1])
    noise_1_list.append(n1)
    # noise_5 (레거시: idx>4일 때만 append, 초기 4개 0 유지)
    if idx > 4:
        noise_5_list.append(abs(H2S_raw_ppm[idx] - H2S_raw_ppm[idx - 5]))

    if idx <= bm:
        if idx == 9 or (idx > 0 and idx % 10 == 0):
            print(f"[gpio_controller] [update_feces_st] 조기반환: idx<=bm (idx={idx} bm={bm})", file=sys.stderr)
        return feces_st, noise_1_list, noise_5_list

    # append 직후 '현재' 값은 마지막 원소 (인덱스 len-1). temp_stt 기준이면 항상 skip되던 버그 수정.
    cur_1 = len(noise_1_list) - 1
    cur_5 = len(noise_5_list) - 1
    if cur_1 < 0 or cur_5 < 0:
        log.info("update_feces_st: skip 판정 (noise_1 len=%s noise_5 len=%s cur_1=%s cur_5=%s)", len(noise_1_list), len(noise_5_list), cur_1, cur_5)
        if idx % 10 == 0:
            print(f"[gpio_controller] [update_feces_st] 조기반환: skip 판정 (cur_1={cur_1} cur_5={cur_5} len_n1={len(noise_1_list)} len_n5={len(noise_5_list)})", file=sys.stderr)
        return feces_st, noise_1_list, noise_5_list

    slice_n1 = noise_1_list[0 : temp_stt - 2]
    max_n1_prev = max(slice_n1) if len(slice_n1) > 0 else 0
    if max_n1_prev > 0.006:
        if noise_1_list[cur_1] > max_n1_prev * 1.2:
            feces_st = idx - 2
    else:
        if noise_1_list[cur_1] > NOISE_1_THRESHOLD:
            feces_st = temp_stt - 2

    slice_n5 = noise_5_list[0:cur_5]
    max_n5_prev = max(slice_n5) if len(slice_n5) > 0 else 0
    if max_n5_prev > NOISE_5_THRESHOLD_HIGH:
        if noise_5_list[cur_5] > max_n5_prev * 1.2:
            feces_st = idx - 2
    else:
        if noise_5_list[cur_5] > NOISE_5_THRESHOLD:
            feces_st = idx - 2

    if feces_st != 0:
        log.info("update_feces_st: feces_st detected idx=%s -> feces_st=%s (noise_1=%.4f noise_5=%.4f)", idx, feces_st, noise_1_list[cur_1], noise_5_list[cur_5])
        n1_val = noise_1_list[cur_1]
        n5_val = noise_5_list[cur_5]
        print(f"[gpio_controller] [update_feces_st] 감지됨 idx={idx} -> feces_st={feces_st} (noise_1={n1_val:.4f} noise_5={n5_val:.4f})", file=sys.stderr)
    elif idx % 10 == 0:
        n1 = noise_1_list[cur_1]
        n5 = noise_5_list[cur_5]
        print(f"[gpio_controller] [update_feces_st] 판정 후 유지 idx={idx} feces_st=0 (noise_1={n1:.4f} noise_5={n5:.4f}, 임계값 n1>{NOISE_1_THRESHOLD} n5>{NOISE_5_THRESHOLD})", file=sys.stderr)
    return feces_st, noise_1_list, noise_5_list


def _trapz(y, x):
    """사다리꼴 적분. numpy 없으면 수동 계산."""
    if _HAS_NUMPY:
        result = float(np.trapz(y, x))
    else:
        s = 0.0
        for i in range(1, len(y)):
            s += (x[i] - x[i - 1]) * (y[i] + y[i - 1]) / 2.0
        result = s
    log.debug("_trapz: len=%s -> result=%.4f", len(y), result)
    return result


def compute_exposure(H2S_raw_ppm_shift, VOCs_raw_ppm_shift, Time_shift, BM_time=None):
    """
    시프트된 구간에서 오프셋 PPM · 절대값 적분 · 비율 계산.
    레거시 MainCode.py 358~377행과 동일: 오프셋 = raw[i] - raw[BM_time], 베이스라인 = raw[BM_time].
    :param H2S_raw_ppm_shift: feces_st-BM_time 부터의 H2S PPM 리스트
    :param VOCs_raw_ppm_shift: 동일 길이 VOCs PPM 리스트
    :param Time_shift: 동일 길이 시간 리스트 (초)
    :param BM_time: BM_time (기본 모듈 상수)
    :return: dict with h2s_abs_exposure, vocs_abs_exposure, total_abs_exposure,
             h2s_ratio_value_pct, vocs_ratio_value_pct, H2S_offseted_ppm_abs, VOCs_offseted_ppm_abs,
             h2s_baseline_ppm, vocs_baseline_ppm (스키마 h2s_offset_ppm/vocs_offset_ppm 용)
    """
    bm = BM_time if BM_time is not None else BM_TIME
    n = len(H2S_raw_ppm_shift)
    log.debug("compute_exposure: n=%s bm=%s", n, bm)
    if n <= bm:
        log.warning("compute_exposure: insufficient data n<=bm, returning 0")
        return {
            "h2s_abs_exposure": 0.0,
            "vocs_abs_exposure": 0.0,
            "total_abs_exposure": 0.0,
            "h2s_ratio_value_pct": 0.0,
            "vocs_ratio_value_pct": 0.0,
            "H2S_offseted_ppm_abs": [0.0] * n,
            "VOCs_offseted_ppm_abs": [0.0] * n,
            "h2s_baseline_ppm": 0.0,
            "vocs_baseline_ppm": 0.0,
        }

    if _HAS_NUMPY:
        time_arr = np.array(Time_shift, dtype=float)
        h2s_off = np.array([0.0] * bm + [H2S_raw_ppm_shift[i] - H2S_raw_ppm_shift[bm] for i in range(bm, n)], dtype=float)
        vocs_off = np.array([0.0] * bm + [VOCs_raw_ppm_shift[i] - VOCs_raw_ppm_shift[bm] for i in range(bm, n)], dtype=float)
        h2s_abs = np.abs(h2s_off)
        vocs_abs = np.abs(vocs_off)
        h2s_exp = float(np.trapz(h2s_abs, time_arr))
        vocs_exp = float(np.trapz(vocs_abs, time_arr))
    else:
        h2s_off = [0.0] * bm + [H2S_raw_ppm_shift[i] - H2S_raw_ppm_shift[bm] for i in range(bm, n)]
        vocs_off = [0.0] * bm + [VOCs_raw_ppm_shift[i] - VOCs_raw_ppm_shift[bm] for i in range(bm, n)]
        h2s_abs = [abs(x) for x in h2s_off]
        vocs_abs = [abs(x) for x in vocs_off]
        h2s_exp = _trapz(h2s_abs, Time_shift)
        vocs_exp = _trapz(vocs_abs, Time_shift)
        h2s_abs_list = h2s_abs
        vocs_abs_list = vocs_abs

    total = h2s_exp + vocs_exp
    h2s_ratio = (100.0 * h2s_exp / total) if total else 0.0
    vocs_ratio = (100.0 * vocs_exp / total) if total else 0.0

    if _HAS_NUMPY:
        h2s_abs_list = h2s_abs.tolist()
        vocs_abs_list = vocs_abs.tolist()

    # 베이스라인: 오프셋 계산 시 빼는 기준값
    h2s_baseline_ppm = float(H2S_raw_ppm_shift[bm])
    vocs_baseline_ppm = float(VOCs_raw_ppm_shift[bm])

    log.info("compute_exposure done: h2s_exp=%.4f vocs_exp=%.4f total=%.4f h2s_ratio=%.2f%% vocs_ratio=%.2f%%",
             h2s_exp, vocs_exp, total, h2s_ratio, vocs_ratio)
    return {
        "h2s_abs_exposure": h2s_exp,
        "vocs_abs_exposure": vocs_exp,
        "total_abs_exposure": total,
        "h2s_ratio_value_pct": h2s_ratio,
        "vocs_ratio_value_pct": vocs_ratio,
        "H2S_offseted_ppm_abs": h2s_abs_list,
        "VOCs_offseted_ppm_abs": vocs_abs_list,
        "h2s_baseline_ppm": h2s_baseline_ppm,
        "vocs_baseline_ppm": vocs_baseline_ppm,
    }


def build_measurement_json(gas_id, test_id, success, wifi_connection,
                           H2S_raw_ppm_shift, VOCs_raw_ppm_shift, Time_shift,
                           calc_result, gas_version="GV.1.1"):
    """
    레거시 형식 JSON (OrderedDict) 생성.
    calc_result: compute_exposure() 반환값.
    """
    data_list = []
    h2s_abs = calc_result["H2S_offseted_ppm_abs"]
    vocs_abs = calc_result["VOCs_offseted_ppm_abs"]
    for i in range(len(H2S_raw_ppm_shift)):
        data_list.append(OrderedDict([
            ("sort", str(i)),
            ("time[sec]", str(Time_shift[i])),
            ("H2S[ppm]", str(H2S_raw_ppm_shift[i])),
            ("VOCs[ppm]", str(VOCs_raw_ppm_shift[i])),
            ("H2S_offset[ppm]", str(h2s_abs[i]) if i < len(h2s_abs) else "0"),
            ("VOCs_offset[ppm]", str(vocs_abs[i]) if i < len(vocs_abs) else "0"),
        ]))

    json_data = OrderedDict()
    json_data["gas_id"] = gas_id
    json_data["test_id"] = test_id
    json_data["success"] = success
    json_data["wifi_connection"] = wifi_connection
    json_data["gas_version"] = gas_version
    json_data["calc_vals"] = OrderedDict([
        ("H2S_abs_exposure", str(calc_result["h2s_abs_exposure"])),
        ("VOCs_abs_exposure", str(calc_result["vocs_abs_exposure"])),
        ("Total_abs_exposure", str(calc_result["total_abs_exposure"])),
        ("H2S_ratio_value[%]", str(calc_result["h2s_ratio_value_pct"])),
        ("VOCs_ratio_value[%]", str(calc_result["vocs_ratio_value_pct"])),
    ])
    json_data["data"] = OrderedDict([("gasValue", data_list)])
    log.debug("build_measurement_json: gas_id=%s test_id=%s success=%s data_len=%s", gas_id, test_id, success, len(data_list))
    return json_data


# ----- 팬 제어 (Raspberry Pi GPIO, 선택 사용) -----
def fan_start(duty_cycle_pct=None, pin=None, frequency_hz=None):
    """PWM 팬 시작. RPi.GPIO 사용 가능 시에만 동작."""
    log.info("[GPIO] fan_start 진입")
    try:
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM)
        p = pin if pin is not None else FAN_PIN
        f = frequency_hz if frequency_hz is not None else FAN_FREQUENCY_HZ
        dc = duty_cycle_pct if duty_cycle_pct is not None else FAN_DUTY_CYCLE_PCT
        GPIO.setup(p, GPIO.OUT)
        pwm = GPIO.PWM(p, f)
        pwm.start(dc)
        log.info("[GPIO] fan_start 성공: pin=%s %sHz duty=%s%%", p, f, dc)
        return pwm
    except Exception as e:
        log.warning("[GPIO] fan_start 실패: %s", e)
        return None


def fan_stop(pwm_or_pin, pin=None):
    """PWM 정지 후 LOW. pwm_or_pin이 PWM 객체면 stop(), 정수면 핀 번호로 LOW."""
    try:
        import RPi.GPIO as GPIO
        if hasattr(pwm_or_pin, "stop"):
            pwm_or_pin.stop()
        p = pin if pin is not None else (pwm_or_pin if isinstance(pwm_or_pin, int) else FAN_PIN)
        GPIO.output(p, GPIO.LOW)
        log.info("[GPIO] fan_stop: pin=%s LOW", p)
    except Exception as e:
        log.warning("[GPIO] fan_stop 예외(무시): %s", e)


def cleanup_gpio():
    """
    Ctrl+C 등 종료 시 gas_controller에서 사용한 GPIO를 안정화.
    팬 핀(FAN_PIN)을 LOW로 두고 해제. RPi.GPIO 미사용 환경에서는 무시.
    """
    try:
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM)
        fan_stop(FAN_PIN)  # PWM 없이도 핀 번호만으로 LOW 출력
        try:
            GPIO.cleanup(FAN_PIN)
        except (TypeError, AttributeError):
            GPIO.cleanup()  # 구버전은 cleanup(channel) 미지원
        log.info("[GPIO] cleanup_gpio: FAN_PIN=%s 안정화 완료", FAN_PIN)
    except ImportError:
        pass
    except Exception as e:
        log.warning("[GPIO] cleanup_gpio 예외(무시): %s", e)


# ----- ADC 읽기 (ABE ADCPi, 선택 사용) -----
# 참고: ABElectronics 라이브러리 — 공식(ADCPi) 또는 레거시(ABE_helpers/ABE_ADCPi) 지원.
# 공식 저장소: https://github.com/abelectronicsuk/ABElectronics_Python_Libraries (Python 3 전용)
# 경로: pip 설치 시 불필요. 미설치 시 ABELECTRONICS_LIB_PATH 또는 config.ABELECTRONICS_LIB_PATH 에
# 라이브러리 루트(ADCPi 폴더의 부모) 지정. 문서: docs/ABELECTRONICS_ADCPi_SETUP.md
def init_adc():
    """ADCPi 초기화. 실패 시 None 반환. 새 API(ADCPi) 우선, 구 API(ABE_helpers/ABE_ADCPi) 폴백."""
    log.info("[GPIO/ADC] init_adc 진입")
    lib_path = os.environ.get("ABELECTRONICS_LIB_PATH", "").strip() or None
    if not lib_path:
        try:
            import config as _cfg
            lib_path = getattr(_cfg, "ABELECTRONICS_LIB_PATH", None)
        except Exception:
            pass
    if lib_path and os.path.isdir(lib_path) and lib_path not in sys.path:
        sys.path.insert(0, lib_path)
        log.debug("[GPIO/ADC] sys.path에 추가: %s", lib_path)

    # 1) 새 API (공식): from ADCPi import ADCPi, ADCPi(addr, addr2, bitrate)
    try:
        from ADCPi import ADCPi
        log.debug("[GPIO/ADC] ADCPi(새 API) import 성공")
        adc = ADCPi(0x68, 0x69, 18)
        adc.set_conversion_mode(0)
        log.info("[GPIO/ADC] init_adc 성공: ADCPi 초기화 완료 (새 API)")
        return adc
    except ImportError:
        pass
    except Exception as e:
        log.warning("[GPIO/ADC] init_adc(새 API) 실패: %s", e)

    # 2) 구 패키지 구조: ADCPi/ABE_ADCPi.py (cannot import name 'ADCPi' from 'ADCPi' 대응)
    try:
        from ADCPi.ABE_ADCPi import ADCPi as _ADCPi
        try:
            from ABE_helpers import ABEHelpers
        except ImportError:
            from ADCPi.ABE_helpers import ABEHelpers
        log.debug("[GPIO/ADC] ADCPi.ABE_ADCPi import 성공")
        i2c_helper = ABEHelpers()
        bus = i2c_helper.get_smbus()
        adc = _ADCPi(bus, 0x68, 0x69, 18)
        adc.set_conversion_mode(0)
        log.info("[GPIO/ADC] init_adc 성공: ADCPi 초기화 완료 (ADCPi.ABE_ADCPi)")
        return adc
    except ImportError:
        pass
    except Exception as e:
        log.warning("[GPIO/ADC] init_adc(ADCPi.ABE_ADCPi) 실패: %s", e)

    # 3) 구 API (플랫): ABE_helpers + ABE_ADCPi (루트에 모듈 있는 구조), bus 전달
    try:
        from ABE_helpers import ABEHelpers
        from ABE_ADCPi import ADCPi
        log.debug("[GPIO/ADC] ABE_helpers, ABE_ADCPi import 성공")
        i2c_helper = ABEHelpers()
        bus = i2c_helper.get_smbus()
        adc = ADCPi(bus, 0x68, 0x69, 18)
        adc.set_conversion_mode(0)
        log.info("[GPIO/ADC] init_adc 성공: ADCPi 초기화 완료 (구 API 플랫)")
        return adc
    except ImportError as e:
        log.warning("[GPIO/ADC] init_adc 실패(모듈 없음): %s — ADCPi/ABE_* 미설치. docs/ABELECTRONICS_ADCPi_SETUP.md 참고.", e)
        return None
    except Exception as e:
        log.warning("[GPIO/ADC] init_adc 실패: %s", e)
        return None


def read_adc_voltages(adc, ch_h2s=1, ch_vocs=2, ch_switch=8):
    """ADC 채널 전압 읽기. adc가 None이면 (0,0,0) 반환."""
    if adc is None:
        log.debug("[GPIO/ADC] read_adc_voltages: adc=None -> (0,0,0)")
        return 0.0, 0.0, 0.0
    try:
        h2s = adc.read_voltage(ch_h2s)
        vocs = adc.read_voltage(ch_vocs)
        sw = adc.read_voltage(ch_switch)
        log.debug("[GPIO/ADC] read_adc: H2S=%.4fV VOCs=%.4fV switch=%.4fV", float(h2s), float(vocs), float(sw))
        return float(h2s), float(vocs), float(sw)
    except Exception as e:
        log.warning("[GPIO/ADC] read_adc_voltages 예외: %s", e)
        return 0.0, 0.0, 0.0


# ----- 명령어 1회 수신 시 1회 실행 (레거시와 동일 처리 순서, 시계열 대기 없음) -----
# 실제 종료는 feces_st + END_TR(180)에서 break. 500회 ≈ 1초/샘플 시 약 8분 상한(정상 시 200~250회).
MEASURE_SEQUENCE_MAX_ITER = int(os.environ.get("MEASURE_SEQUENCE_MAX_ITER", "500"))


def measure_sequence(gas_id, test_id, capture_callback=None, simulation=False, pwm=None, api_base=None):
    """
    명령어 기반 1회 실행. 레거시 MainCode와 동일한 처리 순서로 동작.

    시나리오 (MEASURE_LOOP_INTERVAL_SEC=1.0 기준):
    - idx==0: fan_stop 후 ADC 읽기 진입 시 fan_start.
    - idx > BM_TIME(8): device status → detecting (1회).
    - idx >= 20: fan_stop, device status → measuring (1회), 이후 MEASURE_SEQUENCE_MAX_ITER 계속.
    1) 호출 직후: 실시간 ADC 측정 루프 진입. 매 루프마다 ADC 읽기 → filter → PPM append.
    2) 8초간 베이스라인: 루프 주기가 1초이면 최소 8샘플 = 8초 분량 수집 후 update_feces_st에서만 감지 가능(idx>BM_time).
    3) 가스 감지 후: feces_st 설정 시점부터 추가로 end_tr(기본 180)샘플 = 3분 측정 후 종료.
    4) 오프셋 재계산: 시프트 구간 = [feces_st-BM_time .. 끝]. 베이스라인 = raw_ppm_shift[BM_time](감지 시점 1개, 레거시와 동일).
       compute_exposure에서 오프셋 = raw[i]-raw[BM_time], trapz 적분·비율 계산.

    순서: ADC 읽기 → utils.filter(또는 filter_voltage) → H2S/VOCs PPM append
          → smooth_peak_h2s → update_feces_st → idx==feces_st+end_tr 시 종료
          → 시프트·오프셋·trapz·비율 계산.
    - capture_callback(slot, data_file_name, image_time_str): slot 1,2,3 촬영 시점에 호출.
    - pwm: 외부에서 넘기면 루프 시작 시 idx==0에서 fan_stop 후 무시하고, ADC 진입 시 내부에서 fan_start. None이면 내부에서 전부 제어.
    - api_base: None이면 config.DATA_API_URL 사용. device status(detecting/measuring) 갱신 시 사용.
    """
    log.info("[GPIO] measure_sequence 시작: gas_id=%s test_id=%s simulation=%s", gas_id, test_id, simulation)

    if simulation:
        log.debug("[GPIO] 시뮬레이션 모드 -> measure_sequence_simulation() 반환")
        return measure_sequence_simulation()

    adc = init_adc()
    if adc is None:
        print("[gpio_controller] measure_sequence: ADC 초기화 실패(ABE_helpers/ADCPi 미사용) -> 가스 루프 생략, 시뮬 결과 반환. 이 경우 슬롯 1,2,3 촬영 없음(0번만 촬영됨).", file=sys.stderr)
        log.warning("[GPIO] measure_sequence: ADC 초기화 실패(ABE_helpers/ADCPi 미사용) -> 가스 루프 생략, 시뮬 결과 반환. 이 경우 슬롯 1,2,3 촬영 없음(0번만 촬영됨).")
        return measure_sequence_simulation()

    if api_base is None and config is not None:
        api_base = getattr(config, "DATA_API_URL", None)

    data_file_name = f"{gas_id}{test_id}"
    log.info("[GPIO] data_file_name=%s use_legacy_filter=%s CAPTURE_IDX_OFFSETS=%s", data_file_name, legacy_filter is not None, CAPTURE_IDX_OFFSETS)
    use_legacy_filter = legacy_filter is not None
    H2S_raw_ppm, VOCs_raw_ppm = [], []
    TIME = []
    noise_1_list = [0]
    noise_5_list = [0.0] * 4
    H2S_a, H2S_b, VOCs_a, VOCs_b = 0.0, 0.0, 0.0, 0.0
    idx = 0
    feces_st = 0
    bm = BM_TIME
    end_tr = END_TR

    gc.collect()
    log.info("[GPIO] 측정 루프 진입 MAX_ITER=%s (feces_st 감지 후 idx가 feces_st+%s에 도달하면 슬롯 1,2,3 촬영)", MEASURE_SEQUENCE_MAX_ITER, CAPTURE_IDX_OFFSETS)
    log.info("[GPIO] 루프 주기=%.1f초/샘플 → 최소 %s초 후 feces_st 판정, 감지 시 추가 %s샘플(약 %.0f초) 후 종료", MEASURE_LOOP_INTERVAL_SEC, bm, end_tr, end_tr * MEASURE_LOOP_INTERVAL_SEC)

    status_detecting_sent = False
    status_measuring_sent = False
    stop_requested = False

    try:
        for _ in range(MEASURE_SEQUENCE_MAX_ITER):
            # device status 폴링: stop 수신 시 루프 탈출 후 프로세스 종료 (subscriber가 PATCH stop 후 재시작)
            if api_base and get_current_status is not None:
                try:
                    current = get_current_status(api_base, gas_id)
                    if current == STATUS_STOP:
                        stop_requested = True
                        log.info("[GPIO] device status=stop 수신 → 측정 루프 조기 종료")
                        print("[gpio_controller] [GPIO] device status=stop 수신, 측정 루프 조기 종료", file=sys.stderr)
                        break
                except Exception as e:
                    log.debug("[GPIO] device status 조회 실패(무시): %s", e)

            start_time = time.time()

            # 1) idx==0: fan_stop 후 ADC 읽기 진입 시 fan_start
            if idx == 0:
                if pwm is not None:
                    fan_stop(pwm)
                    pwm = None
                else:
                    fan_stop(FAN_PIN)
                try:
                    pwm = fan_start()
                    log.info("[GPIO] ADC 읽기 진입 시 fan_start (pin=%s)", FAN_PIN)
                except Exception as e:
                    log.warning("[GPIO] fan_start 예외: %s", e)
                    pwm = None

            # 2) idx > BM_TIME(8): device status → detecting (1회)
            if idx > bm and not status_detecting_sent:
                if api_base and ensure_ready_then_set is not None:
                    try:
                        ensure_ready_then_set(api_base, gas_id, STATUS_DETECTING)
                        log.info("[GPIO] Device status 갱신: detecting (idx>BM_TIME)")
                        print("[SCENARIO] 7. Device status 갱신: detecting (gas_controller)", file=sys.stderr)
                        # INSERT_YOUR_CODE
                        time.sleep(8)
                        if api_base and get_current_status is not None:
                            try:
                                if get_current_status(api_base, gas_id) == STATUS_STOP:
                                    stop_requested = True
                                    break
                            except Exception:
                                pass
                    except Exception as e:
                        log.warning("[GPIO] device status detecting 전송 실패: %s", e)
                status_detecting_sent = True

            # 3) idx >= 20: fan_stop, device status → measuring (1회), 이후 루프 계속
            if idx >= 20 and not status_measuring_sent:
                if pwm is not None:
                    fan_stop(pwm)
                    pwm = None
                if api_base and update_device_status is not None:
                    try:
                        update_device_status(api_base, gas_id, STATUS_MEASURING)
                        log.info("[GPIO] Device status 갱신: measuring (idx>=20)")
                        print("[SCENARIO] 8. Device status 갱신: measuring (gas_controller)", file=sys.stderr)
                    except Exception as e:
                        log.warning("[GPIO] device status measuring 전송 실패: %s", e)
                status_measuring_sent = True

            # 4) ADC 읽기
            h2s_v, vocs_v, _ = read_adc_voltages(adc)
            if idx == 0:
                log.info("[GPIO] 첫 ADC 읽기: H2S=%.4fV VOCs=%.4fV", h2s_v, vocs_v)
                print("[gpio_controller] [GPIO] 가스 루프 1회차 ADC 읽기 완료 (이후 약 1초/샘플로 진행, feces_st 감지 시 슬롯 1,2,3 촬영)", file=sys.stderr)
                print(f"[gpio_controller] [ADC] idx=0 H2S={h2s_v:.4f}V VOCs={vocs_v:.4f}V", file=sys.stderr)

            # 2) utils.filter 또는 filter_voltage → 필터 출력
            if use_legacy_filter:
                H2S_filtered_v, H2S_b, H2S_a = legacy_filter(h2s_v, H2S_b, H2S_a)
                VOCs_filtered_v, VOCs_b, VOCs_a = legacy_filter(vocs_v, VOCs_b, VOCs_a)
            else:
                H2S_filtered_v, H2S_b, _ = filter_voltage(h2s_v, H2S_b)
                VOCs_filtered_v, VOCs_b, _ = filter_voltage(vocs_v, VOCs_b)
            
            # 3) H2S_RAW_PPM / VOCs_RAW_PPM append
            H2S_RAW_PPM = (float(H2S_filtered_v) - VOLTAGE_OFFSET) * 1e6 / H2S_DIVISOR if H2S_DIVISOR else 0.0
            VOCs_RAW_PPM = (float(VOCs_filtered_v) - VOLTAGE_OFFSET) * 1e6 / VOCS_DIVISOR if VOCS_DIVISOR else 0.0
            H2S_raw_ppm.append(H2S_RAW_PPM)
            VOCs_raw_ppm.append(VOCs_RAW_PPM)

            # ADC 로그: 10샘플마다 전압·PPM 출력 (idx 0은 위에서 이미 출력)
            if idx > 0 and idx % 10 == 0:
                print(f"[gpio_controller] [ADC] idx={idx} H2S={h2s_v:.4f}V VOCs={vocs_v:.4f}V -> PPM H2S={H2S_RAW_PPM:.4f} VOCs={VOCs_RAW_PPM:.4f}", file=sys.stderr)

            # 4) smooth_peak_h2s, 5) update_feces_st (feces_st==0이고 idx>1일 때만)
            if feces_st == 0 and idx > 1:
                temp_stt = idx - 1
                smooth_peak_h2s(H2S_raw_ppm, temp_stt)
                feces_st, noise_1_list, noise_5_list = update_feces_st(idx, H2S_raw_ppm, noise_1_list, noise_5_list, feces_st, bm)
                if feces_st != 0:
                    print("[GPIO] feces_st 감지: idx=%s -> feces_st=%s (이후 idx=%s,%s,%s에서 슬롯 1,2,3 촬영)", idx, feces_st, feces_st + CAPTURE_IDX_OFFSETS[0], feces_st + CAPTURE_IDX_OFFSETS[1], feces_st + CAPTURE_IDX_OFFSETS[2])
                    time.sleep(0.5)

            # Feces 슬롯 1,2,3 촬영 시점 (idx == feces_st + CAPTURE_IDX_OFFSETS[0|1|2] 일 때, 기본 30/60/120)
            if capture_callback and feces_st != 0:
                for slot_one_based, offset in enumerate(CAPTURE_IDX_OFFSETS, start=1):
                    if idx == feces_st + offset:
                        image_time_str = datetime.now().strftime("%Y%m%d%H%M%S")
                        log.info("[GPIO] 캡처 요청 slot=%s idx=%s (feces_st+offset=%s) image_time=%s", slot_one_based, idx, offset, image_time_str)
                        capture_callback(slot_one_based, data_file_name, image_time_str)
                        break
            elif idx == MEASURE_SEQUENCE_MAX_ITER:
                if api_base and update_device_status is not None:
                    try:
                        update_device_status(api_base, gas_id, STATUS_FAIL)
                        log.info("[GPIO] Device status 갱신: fail (캡처 시점 slot=%s)", slot_one_based)
                    except Exception as e:
                        log.warning("[GPIO] device status fail 전송 실패: %s", e)

            end_time = time.time()
            if idx == 0:
                TIME.append(f"{end_time - start_time:.2f}")
            else:
                TIME.append(f"{float(TIME[idx - 1]) + end_time - start_time:.2f}")

            # 6) idx == feces_st + end_tr 시 종료
            if feces_st != 0 and idx == feces_st + end_tr:
                log.info("[GPIO] 측정 루프 종료 조건: idx=%s feces_st=%s end_tr=%s", idx, feces_st, end_tr)
                break
            idx += 1

            # 진행 로그: 초반(1,5,10) 및 10샘플마다 stderr 출력 (idx 60 이후에도 70,80,90... 계속 출력되도록 idx<=60 제거)
            if idx == 1 or idx == 5 or idx == 10:
                log.info("[GPIO] 루프 진행 idx=%s feces_st=%s H2S=%.4f VOCs=%.4f (정상 동작 중)", idx, feces_st, H2S_raw_ppm[-1] if H2S_raw_ppm else 0, VOCs_raw_ppm[-1] if VOCs_raw_ppm else 0)
                print(f"[gpio_controller] [GPIO] 가스 루프 진행 idx={idx} feces_st={feces_st} (정상)", file=sys.stderr)
            elif idx > 0 and idx % 10 == 0:
                print(f"[gpio_controller] [GPIO] 가스 루프 진행 idx={idx} feces_st={feces_st}", file=sys.stderr)
                log.info("[GPIO] 루프 진행 idx=%s feces_st=%s H2S=%.4f VOCs=%.4f", idx, feces_st, H2S_raw_ppm[-1] if H2S_raw_ppm else 0, VOCs_raw_ppm[-1] if VOCs_raw_ppm else 0)
            elif idx > 0 and idx % 50 == 0:
                log.info("[GPIO] 루프 진행 idx=%s feces_st=%s H2S_last=%.4f VOCs_last=%.4f", idx, feces_st, H2S_raw_ppm[-1] if H2S_raw_ppm else 0, VOCs_raw_ppm[-1] if VOCs_raw_ppm else 0)
            elif idx > 0 and idx % 200 == 0:
                log.debug("[GPIO] 루프 진행 idx=%s feces_st=%s", idx, feces_st)

            # 루프 주기: 1Hz(1초/샘플) 목표. elapsed 보정으로 매 iteration을 MEASURE_LOOP_INTERVAL_SEC(1초)에 맞춤.
            # (고정 time.sleep(1)은 처리시간+1초가 되어 주기가 늘어나므로 사용하지 않음. elapsed>1초면 sleep 없음 → 주기 초과 시 로그)
            if MEASURE_LOOP_INTERVAL_SEC > 0:
                elapsed = time.time() - start_time
                sleep_sec = MEASURE_LOOP_INTERVAL_SEC - elapsed
                if sleep_sec > 0:
                    time.sleep(sleep_sec)
                    if api_base and get_current_status is not None:
                        try:
                            if get_current_status(api_base, gas_id) == STATUS_STOP:
                                stop_requested = True
                                break
                        except Exception:
                            pass
                elif idx > 0 and idx % 50 == 0:
                    print("[GPIO] 루프 주기 초과 idx=%s elapsed=%.2fs (API/캡처 지연 시 전체 측정 시간 증가)", idx, elapsed)
    finally:
        if pwm is not None:
            fan_stop(pwm)
            log.info("[GPIO] 팬 PWM 정지 완료")
        log.info("[GPIO] 측정 루프 종료 idx=%s len(H2S_raw_ppm)=%s", idx, len(H2S_raw_ppm))
        if stop_requested:
            if api_base and update_device_status is not None:
                try:
                    update_device_status(api_base, gas_id, STATUS_READY)
                    log.info("[GPIO] stop 종료 전 device status → ready (재실행 대기)")
                    print("[gpio_controller] [GPIO] stop 종료 전 device status → ready (재실행 대기)", file=sys.stderr)
                except Exception as e:
                    log.warning("[GPIO] device status ready 전송 실패: %s", e)
            sys.exit(0)

    # 종료 후: 시프트·오프셋·trapz·비율 계산
    if feces_st == 0 or feces_st < bm or feces_st + end_tr > len(H2S_raw_ppm):
        log.warning("[GPIO] 측정 구간 무효 (feces_st=%s bm=%s len=%s) -> 시뮬 결과 반환", feces_st, bm, len(H2S_raw_ppm))
        return measure_sequence_simulation()

    # TIME과 H2S_raw_ppm 길이 불일치 시 (루프 중 예외 등) 시뮬 반환하여 list index out of range 방지
    if len(TIME) != len(H2S_raw_ppm):
        log.warning("[GPIO] TIME/H2S_raw_ppm 길이 불일치 (TIME=%s H2S=%s) -> 시뮬 결과 반환", len(TIME), len(H2S_raw_ppm))
        return measure_sequence_simulation()

    H2S_raw_ppm_shift, VOCs_raw_ppm_shift, Time_shift = [], [], []
    for i in range(feces_st - bm, len(H2S_raw_ppm)):
        H2S_raw_ppm_shift.append(H2S_raw_ppm[i])
        VOCs_raw_ppm_shift.append(VOCs_raw_ppm[i])
        Time_shift.append(f"{float(TIME[i]) - float(TIME[feces_st - bm]):.2f}")
    log.debug("measure_sequence: shift range built shift_len=%s", len(H2S_raw_ppm_shift))

    # 대용량 리스트 조기 해제 후 GC (ref MainCode 352~355행). 1~2GB RAM 환경 완화.
    del H2S_raw_ppm, VOCs_raw_ppm, TIME, noise_1_list, noise_5_list
    gc.collect()
    time.sleep(0.1)

    calc_result = compute_exposure(H2S_raw_ppm_shift, VOCs_raw_ppm_shift, [float(x) for x in Time_shift], bm)
    gc.collect()
    n = len(H2S_raw_ppm_shift)
    last_h2s = H2S_raw_ppm_shift[-1] if n else 0.0
    last_vocs = VOCs_raw_ppm_shift[-1] if n else 0.0
    time_sec = float(Time_shift[-1]) if Time_shift else 0.0

    # h2s_offset_ppm / vocs_offset_ppm: 베이스라인 기준값 (오프셋 시 빼는 raw[BM_time]). MainCode.py 364~365행 대응.
    h2s_baseline = calc_result.get("h2s_baseline_ppm", 0.0)
    vocs_baseline = calc_result.get("vocs_baseline_ppm", 0.0)

    print("measure_sequence done: sort=%s time_sec=%.2f h2s_ppm=%.4f vocs_ppm=%.4f total_abs_exposure=%.4f",n, time_sec, last_h2s, last_vocs, calc_result["total_abs_exposure"])

    return {
        "gas_version": "GV.1.1",
        "h2s_abs_exposure": calc_result["h2s_abs_exposure"],
        "h2s_offset_ppm": h2s_baseline,
        "h2s_ppm": last_h2s,
        "h2s_ratio_value_pct": calc_result["h2s_ratio_value_pct"],
        "sort": n,
        "success": "Y",
        "time_sec": time_sec,
        "total_abs_exposure": calc_result["total_abs_exposure"],
        "vocs_abs_exposure": calc_result["vocs_abs_exposure"],
        "vocs_offset_ppm": vocs_baseline,
        "vocs_ppm": last_vocs,
        "vocs_ratio_value_pct": calc_result["vocs_ratio_value_pct"],
        "H2S_raw_ppm_shift": H2S_raw_ppm_shift,
        "VOCs_raw_ppm_shift": VOCs_raw_ppm_shift,
        "Time_shift": Time_shift,
        "calc_result": calc_result,
    }


def measure_sequence_simulation():
    """명령어 기반 1회 실행의 시뮬레이션: 동일 스키마 더미 반환."""
    import random
    h2s_exp = round(random.uniform(0, 10), 4)
    vocs_exp = round(random.uniform(0, 10), 4)
    total = h2s_exp + vocs_exp
    h2s_ratio = (100.0 * h2s_exp / total) if total else 0.0
    vocs_ratio = (100.0 * vocs_exp / total) if total else 0.0
    # 시뮬레이션: 시계열/베이스라인 없음 → h2s_offset_ppm / vocs_offset_ppm 은 0.0
    return {
        "gas_version": "0.0.1",
        "h2s_abs_exposure": h2s_exp,
        "h2s_offset_ppm": 0.0,
        "h2s_ppm": round(random.uniform(0, 20), 4),
        "h2s_ratio_value_pct": h2s_ratio,
        "sort": 0,
        "success": "ok",
        "time_sec": 0.0,
        "total_abs_exposure": total,
        "vocs_abs_exposure": vocs_exp,
        "vocs_offset_ppm": 0.0,
        "vocs_ppm": round(random.uniform(0, 50), 4),
        "vocs_ratio_value_pct": vocs_ratio,
        "H2S_raw_ppm_shift": [],
        "VOCs_raw_ppm_shift": [],
        "Time_shift": [],
        "calc_result": None,
    }


# ----- 기존 measure_once API (main.py 호환) -----
def measure_once():
    """
    가스 관련 GPIO·센서 1회 측정 (실측 모드에서 호출).
    실제 하드웨어 연동 시 init_adc() + read_adc_voltages() → filter_voltage → voltage_to_ppm_* 사용.
    :return: dict - DB 스키마와 동일한 키 (gas_id, test_id 제외).
    """
    adc = init_adc()
    if adc is None:
        return measure_once_simulation()

    h2s_v, vocs_v, _ = read_adc_voltages(adc)
    # 1회 읽기만 하므로 필터 상태 0으로
    h2s_f, _, _ = filter_voltage(h2s_v, 0.0)
    vocs_f, _, _ = filter_voltage(vocs_v, 0.0)
    h2s_ppm = voltage_to_ppm_h2s(h2s_f)
    vocs_ppm = voltage_to_ppm_vocs(vocs_f)

    # 1회 측정: 시계열·BM_time 없음 → 베이스라인 개념 없음. h2s_offset_ppm / vocs_offset_ppm 은 None (또는 0.0).
    return {
        "gas_version": "GV.1.1",
        "h2s_abs_exposure": None,
        "h2s_offset_ppm": None,
        "h2s_ppm": h2s_ppm,
        "h2s_ratio_value_pct": None,
        "sort": 0,
        "success": "Y",
        "time_sec": 0.0,
        "total_abs_exposure": None,
        "vocs_abs_exposure": None,
        "vocs_offset_ppm": None,
        "vocs_ppm": vocs_ppm,
        "vocs_ratio_value_pct": None,
    }


def measure_once_simulation():
    """시뮬레이션용: 0/더미 값으로 채운 측정 1회. 1회만 읽으므로 베이스라인 없음 → h2s_offset_ppm / vocs_offset_ppm = 0.0."""
    return {
        "gas_version": "0.0.1",
        "h2s_abs_exposure": 0.0,
        "h2s_offset_ppm": 0.0,
        "h2s_ppm": 0.0,
        "h2s_ratio_value_pct": 0.0,
        "sort": 0,
        "success": "ok",
        "time_sec": 0.0,
        "total_abs_exposure": 0.0,
        "vocs_abs_exposure": 0.0,
        "vocs_offset_ppm": 0.0,
        "vocs_ppm": 0.0,
        "vocs_ratio_value_pct": 0.0,
    }
