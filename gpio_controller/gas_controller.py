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
import os
import time
from datetime import datetime
from collections import OrderedDict

try:
    from utils import filter as legacy_filter
except ImportError:
    legacy_filter = None

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

# ----- 레거시 상수 -----
BM_TIME = int(os.environ.get("BM_TIME", "8"))           # baseline 구간 길이
END_TR = int(os.environ.get("END_TR", "180"))           # feces_st + end_tr 에서 측정 종료
SHORT_TR = int(os.environ.get("SHORT_TR", "135"))      # 10+2
LONG_TR = int(os.environ.get("LONG_TR", "20"))         # 5+2
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


def filter_voltage(voltage, b_prev, alpha=0.1):
    """
    1차 저역통과 필터 (지수이동평균).
    filtered = alpha * voltage + (1 - alpha) * b_prev
    :return: (filtered_v, b_new, a_new) — b_new는 다음 스텝의 b_prev로 사용
    """
    filtered = alpha * float(voltage) + (1.0 - alpha) * float(b_prev)
    return filtered, filtered, alpha


def voltage_to_ppm_h2s(voltage):
    """H2S 전압 → PPM (원본 공식)."""
    v = float(voltage) - VOLTAGE_OFFSET
    return (v * 1e6) / H2S_DIVISOR if H2S_DIVISOR else 0.0


def voltage_to_ppm_vocs(voltage):
    """VOCs 전압 → PPM (원본 공식)."""
    v = float(voltage) - VOLTAGE_OFFSET
    return (v * 1e6) / VOCS_DIVISOR if VOCS_DIVISOR else 0.0


def smooth_peak_h2s(H2S_raw_ppm, idx):
    """
    H2S 피크 제거: idx가 극값이면서 양옆 차이 < STABLE_THRE 이면 idx를 이전 값으로 대체.
    :param H2S_raw_ppm: 리스트 (최소 idx+2 길이)
    :param idx: 중간 인덱스 (temp_stt)
    """
    if idx < 1 or idx + 1 >= len(H2S_raw_ppm):
        return
    a, b, c = H2S_raw_ppm[idx - 1], H2S_raw_ppm[idx], H2S_raw_ppm[idx + 1]
    if (b - a) * (c - b) < 0 and abs(c - a) < STABLE_THRE:
        if abs(b - a) * abs(b - c) > 0.005 * 0.005:
            H2S_raw_ppm[idx] = a


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
    bm = BM_time if BM_time is not None else BM_TIME
    if feces_st != 0 or idx <= 1:
        return feces_st, noise_1_list, noise_5_list

    temp_stt = idx - 1
    if temp_stt < 2:
        return feces_st, noise_1_list, noise_5_list

    # noise_1
    n1 = abs(H2S_raw_ppm[idx] - H2S_raw_ppm[idx - 1])
    noise_1_list.append(n1)
    # noise_5 (레거시: idx>4일 때만 append, 초기 4개 0 유지)
    if idx > 4:
        noise_5_list.append(abs(H2S_raw_ppm[idx] - H2S_raw_ppm[idx - 5]))

    if idx <= bm:
        return feces_st, noise_1_list, noise_5_list

    # feces_st 판정
    max_n1_prev = max(noise_1_list[0 : temp_stt - 2]) if temp_stt > 2 else 0
    if max_n1_prev > 0.006:
        if noise_1_list[temp_stt] > max_n1_prev * 1.2:
            feces_st = idx - 2
    else:
        if noise_1_list[temp_stt] > NOISE_1_THRESHOLD:
            feces_st = temp_stt - 2

    max_n5_prev = max(noise_5_list[0 : temp_stt - 2]) if temp_stt > 2 else 0
    if max_n5_prev > NOISE_5_THRESHOLD_HIGH:
        if noise_5_list[temp_stt] > max_n5_prev * 1.2:
            feces_st = idx - 2
    else:
        if noise_5_list[temp_stt] > NOISE_5_THRESHOLD:
            feces_st = idx - 2

    return feces_st, noise_1_list, noise_5_list


def _trapz(y, x):
    """사다리꼴 적분. numpy 없으면 수동 계산."""
    if _HAS_NUMPY:
        return float(np.trapz(y, x))
    s = 0.0
    for i in range(1, len(y)):
        s += (x[i] - x[i - 1]) * (y[i] + y[i - 1]) / 2.0
    return s


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
    if n <= bm:
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
    return json_data


# ----- 팬 제어 (Raspberry Pi GPIO, 선택 사용) -----
def fan_start(duty_cycle_pct=None, pin=None, frequency_hz=None):
    """PWM 팬 시작. RPi.GPIO 사용 가능 시에만 동작."""
    import sys
    try:
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM)
        p = pin if pin is not None else FAN_PIN
        f = frequency_hz if frequency_hz is not None else FAN_FREQUENCY_HZ
        dc = duty_cycle_pct if duty_cycle_pct is not None else FAN_DUTY_CYCLE_PCT
        GPIO.setup(p, GPIO.OUT)
        pwm = GPIO.PWM(p, f)
        pwm.start(dc)
        print(f"[gpio_controller] PWM fan 시작 pin={p} {f}Hz duty={dc}%", file=sys.stderr)
        return pwm
    except Exception:
        return None


def fan_stop(pwm_or_pin, pin=None):
    """PWM 정지 후 LOW. pwm_or_pin이 PWM 객체면 stop(), 정수면 핀 번호로 LOW."""
    try:
        import RPi.GPIO as GPIO
        if hasattr(pwm_or_pin, "stop"):
            pwm_or_pin.stop()
        p = pin if pin is not None else (pwm_or_pin if isinstance(pwm_or_pin, int) else FAN_PIN)
        GPIO.output(p, GPIO.LOW)
    except Exception:
        pass


# ----- ADC 읽기 (ABE ADCPi, 선택 사용) -----
def init_adc():
    """ADCPi 초기화. 실패 시 None 반환."""
    try:
        from ABE_helpers import ABEHelpers
        from ABE_ADCPi import ADCPi
        i2c_helper = ABEHelpers()
        bus = i2c_helper.get_smbus()
        adc = ADCPi(bus, 0x68, 0x69, 18)
        adc.set_conversion_mode(0)
        return adc
    except Exception:
        return None


def read_adc_voltages(adc, ch_h2s=1, ch_vocs=2, ch_switch=8):
    """ADC 채널 전압 읽기. adc가 None이면 (0,0,0) 반환."""
    if adc is None:
        return 0.0, 0.0, 0.0
    try:
        h2s = adc.read_voltage(ch_h2s)
        vocs = adc.read_voltage(ch_vocs)
        sw = adc.read_voltage(ch_switch)
        return float(h2s), float(vocs), float(sw)
    except Exception:
        return 0.0, 0.0, 0.0


# ----- 명령어 1회 수신 시 1회 실행 (레거시와 동일 처리 순서, 시계열 대기 없음) -----
MEASURE_SEQUENCE_MAX_ITER = int(os.environ.get("MEASURE_SEQUENCE_MAX_ITER", "3000"))


def measure_sequence(gas_id, test_id, capture_callback=None, simulation=False, pwm=None):
    """
    명령어 기반 1회 실행. 시계열 대기 없이, 레거시와 동일한 처리 순서로 동작.
    순서: ADC 읽기 → utils.filter(또는 filter_voltage) → H2S/VOCs PPM append
          → smooth_peak_h2s → update_feces_st → idx==feces_st+end_tr 시 종료
          → 시프트·오프셋·trapz·비율 계산.
    - capture_callback(slot, data_file_name, image_time_str): slot 1,2,3 촬영 시점에 호출.
    - pwm: 이미 main 등에서 fan_start()로 켠 PWM 객체를 넘기면, 여기서 fan_start() 생략하고
          루프 종료 시 이 객체로 fan_stop() 호출. None이면 내부에서 fan_start() 후 종료 시 fan_stop().
    """
    if simulation:
        return measure_sequence_simulation()

    adc = init_adc()
    if adc is None:
        return measure_sequence_simulation()

    if pwm is None:
        try:
            pwm = fan_start()
        except Exception:
            pwm = None

    data_file_name = f"{gas_id}{test_id}"
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

    # 라즈베리파이(1~2GB RAM): 측정 루프 진입 전 불필요 메모리 회수 (ref MainCode 162행)
    gc.collect()

    try:
        for _ in range(MEASURE_SEQUENCE_MAX_ITER):
            start_time = time.time()

            # 1) ADC 읽기
            h2s_v, vocs_v, _ = read_adc_voltages(adc)
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

            # 4) smooth_peak_h2s, 5) update_feces_st (feces_st==0이고 idx>1일 때만)
            if feces_st == 0 and idx > 1:
                temp_stt = idx - 1
                smooth_peak_h2s(H2S_raw_ppm, temp_stt)
                feces_st, noise_1_list, noise_5_list = update_feces_st(
                    idx, H2S_raw_ppm, noise_1_list, noise_5_list, feces_st, bm
                )
                if feces_st != 0:
                    time.sleep(0.5)

            # Feces 슬롯 1,2,3 촬영 시점 (idx == feces_st + CAPTURE_IDX_OFFSETS[0|1|2] 일 때, 기본 30/60/120)
            if capture_callback and feces_st != 0:
                for slot_one_based, offset in enumerate(CAPTURE_IDX_OFFSETS, start=1):
                    if idx == feces_st + offset:
                        capture_callback(slot_one_based, data_file_name, datetime.now().strftime("%Y%m%d%H%M%S"))
                        break

            end_time = time.time()
            if idx == 0:
                TIME.append(f"{end_time - start_time:.2f}")
            else:
                TIME.append(f"{float(TIME[idx - 1]) + end_time - start_time:.2f}")

            # 6) idx == feces_st + end_tr 시 종료
            if feces_st != 0 and idx == feces_st + end_tr:
                break
            idx += 1
    finally:
        if pwm is not None:
            fan_stop(pwm)

    # 종료 후: 시프트·오프셋·trapz·비율 계산
    if feces_st == 0 or feces_st < bm or feces_st + end_tr > len(H2S_raw_ppm):
        return measure_sequence_simulation()

    H2S_raw_ppm_shift, VOCs_raw_ppm_shift, Time_shift = [], [], []
    for i in range(feces_st - bm, len(H2S_raw_ppm)):
        H2S_raw_ppm_shift.append(H2S_raw_ppm[i])
        VOCs_raw_ppm_shift.append(VOCs_raw_ppm[i])
        Time_shift.append(f"{float(TIME[i]) - float(TIME[feces_st - bm]):.2f}")

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
