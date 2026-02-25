# -*- coding: utf-8 -*-
"""
gpio_controller 설정 (개발자 입력)
- API 전달용 URL, DB 관련 등 필요 시 추가
"""
import os

# gpio_controller 디렉토리 기준 경로
GPIO_CONTROLLER_DIR = os.path.dirname(os.path.abspath(__file__))

# 측정 데이터를 전달할 API URL (퍼블리셔 서버, /api 와 분리해 /mqtt 경로 사용)
# 예: http://52.78.222.49:3001 또는 http://bidet.hem-sensorbot.com:3001
DATA_API_URL = os.environ.get(
    "DATA_API_URL",
    os.environ.get("API_BASE_URL", "http://52.78.222.49:3001"),
).rstrip("/")

# measurement POST 경로 (기본 /mqtt/api/v1/measurement, 별도 /api 사용 시 변경)
DATA_API_MEASUREMENT_PATH = os.environ.get(
    "DATA_API_MEASUREMENT_PATH",
    "/mqtt/api/v1/measurement",
).strip() or "/mqtt/api/v1/measurement"

# 디바이스 상태 API 경로 (ready → detecting → measuring → completed 갱신용)
DATA_API_DEVICE_STATUS_PATH = os.environ.get(
    "DATA_API_DEVICE_STATUS_PATH",
    "/mqtt/api/v1/device/status",
).strip() or "/mqtt/api/v1/device/status"

# 이미지 분석 결과 전송 API 경로 (image_analysis_table 스키마 포맷)
DATA_API_IMAGE_ANALYSIS_PATH = os.environ.get(
    "DATA_API_IMAGE_ANALYSIS_PATH",
    "/mqtt/api/v1/image_analysis",
).strip() or "/mqtt/api/v1/image_analysis"

# 이미지 분석/전송 서버 URL (ref/MainCode.py HONG_URL 과 동일)
# - 촬영 이미지 multipart POST 전송 (ref/servlet.py: 'file' 수신)
# - 환경변수 IMAGE_UPLOAD_URL 으로 오버라이드 가능
IMAGE_UPLOAD_URL = os.environ.get("IMAGE_UPLOAD_URL", "http://13.209.29.94:5000")

# 이미지 분석 결과 확인 경로 suffix (서버에서 분석 후 결과 조회 시)
# 형식: {IMAGE_ANALYSIS_RESULT_BASE}/{gas_id}/upload/{test_id}
IMAGE_ANALYSIS_RESULT_BASE = os.environ.get(
    "IMAGE_ANALYSIS_RESULT_BASE",
    "image-analysis",
)

# 이미지 분석 결과 GET URL (별도 조회 시 사용. MainCode.py HONG_URL 과 동일 서버면 http://13.209.29.94:5000)
# 비어 있으면 업로드 응답에 분석 결과가 포함된 경우만 사용, 별도 GET 조회 안 함.
# 형식: {IMAGE_ANALYSIS_RESULT_URL}/{gas_id}/upload/{test_id}
IMAGE_ANALYSIS_RESULT_URL = os.environ.get("IMAGE_ANALYSIS_RESULT_URL", "").rstrip("/")

# DB 접근이 gpio_controller 내부에서 필요할 경우 아래에 개발자 입력
# DB_HOST = os.environ.get("DB_HOST", "localhost")
# DB_PORT = int(os.environ.get("DB_PORT", "3306"))
# DB_USER = os.environ.get("DB_USER", "")
# DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
# DB_NAME = os.environ.get("DB_NAME", "")
