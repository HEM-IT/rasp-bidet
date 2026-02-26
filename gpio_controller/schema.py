# -*- coding: utf-8 -*-
"""
측정 데이터 포맷 (DB 스키마와 동일).
- wifi_connection 제외.
- gas_id = device_id (알파벳 5자리, 예: FFFFF).
- test_id = 실제 구동 시 외부에서 전달받음 (숫자 5자리 00000~99999), gpio_controller에서 생성하지 않음.
"""
# DB 컬럼과 1:1 대응하는 키 (JSON 키는 [ppm], [%], [sec] 등 대신 _ppm, _pct, _sec 사용)
MEASUREMENT_KEYS = [
    "profile_id",       # 명령에서 전달
    "gas_id",           # device_id (5자 알파벳)
    "test_id",          # 외부 전달 (5자리 숫자), 미전달 시 None
    "gas_version",
    "h2s_abs_exposure",
    "h2s_offset_ppm",
    "h2s_ppm",
    "h2s_ratio_value_pct",
    "sort",
    "success",
    "time_sec",
    "total_abs_exposure",
    "vocs_abs_exposure",
    "vocs_offset_ppm",
    "vocs_ppm",
    "vocs_ratio_value_pct",
    "created_at",       # 측정 완료 시각 (ISO 8601, API/DB 저장용)
]


def build_empty_measurement():
    """스키마 필드만 넣은 빈 측정 레코드 (값은 None)."""
    return {k: None for k in MEASUREMENT_KEYS}
