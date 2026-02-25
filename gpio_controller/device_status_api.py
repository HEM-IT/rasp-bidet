# -*- coding: utf-8 -*-
"""
디바이스 상태 API 호출 (gpio 작업 시 ready → detecting → measuring → completed 갱신).
- DATA_API_URL 기준으로 GET(생성 여부 조회), POST(최초 생성), PATCH(상태 갱신) 호출.
"""
import os
import sys
import json
import urllib.request
import urllib.error
import urllib.parse
import ssl

import config

# 상태 순서: ready → detecting → measuring → completed
STATUS_READY = "ready"
STATUS_DETECTING = "detecting"
STATUS_MEASURING = "measuring"
STATUS_COMPLETED = "completed"


def _request(method, url, body=None, timeout=10):
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    ctx = ssl.create_default_context()
    if url.startswith("https://"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode()
            data = json.loads(body) if body else None
        except Exception:
            data = None
        return e.code, data
    except Exception as e:
        print(f"[device_status_api] 요청 실패 {method} {url}: {e}", file=sys.stderr)
        raise


def get_device_status(api_base_url, gas_id):
    """
    gas_id에 해당하는 디바이스 상태 생성 여부 조회.
    :return: (status_code, response_body). body.exists == True 이면 이미 존재, False 이면 미생성.
    """
    if not api_base_url or not gas_id:
        return None, None
    url = f"{api_base_url.rstrip('/')}{config.DATA_API_DEVICE_STATUS_PATH}?gas_id={urllib.parse.quote(str(gas_id))}"
    try:
        return _request("GET", url)
    except Exception:
        return None, None


def create_device_status(api_base_url, gas_id, status=STATUS_READY):
    """
    디바이스 상태 최초 생성 (POST). ready 일 때 1회 호출.
    :return: (status_code, response_body). 201 created 또는 200 already exists.
    """
    if not api_base_url or not gas_id:
        return None, None
    url = f"{api_base_url.rstrip('/')}{config.DATA_API_DEVICE_STATUS_PATH}"
    body = json.dumps({"gas_id": gas_id, "status": status}).encode("utf-8")
    try:
        return _request("POST", url, body=body)
    except Exception:
        return None, None


def update_device_status(api_base_url, gas_id, status):
    """
    디바이스 상태 갱신 (PATCH). detecting / measuring / completed 시 호출.
    :return: (status_code, response_body).
    """
    if not api_base_url or not gas_id or not status:
        return None, None
    url = f"{api_base_url.rstrip('/')}{config.DATA_API_DEVICE_STATUS_PATH}"
    body = json.dumps({"gas_id": gas_id, "status": status}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="PATCH", headers={"Content-Type": "application/json"})
    ctx = ssl.create_default_context()
    if url.startswith("https://"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode())
        except Exception:
            data = None
        return e.code, data
    except Exception as e:
        print(f"[device_status_api] PATCH 실패: {e}", file=sys.stderr)
        return None, None


def ensure_ready_then_set(api_base_url, gas_id, next_status):
    """
    GET으로 gas_id 존재 여부 확인 후, 없으면 POST(ready)로 생성하고, 이어서 next_status로 PATCH.
    있으면 바로 next_status로 PATCH.
    :param next_status: STATUS_DETECTING 등 다음 상태.
    """
    code, body = get_device_status(api_base_url, gas_id)
    if code is None:
        return
    if body and body.get("exists"):
        # 이미 있으면 바로 다음 상태로 갱신
        update_device_status(api_base_url, gas_id, next_status)
    else:
        # 없으면 생성 후 갱신
        create_device_status(api_base_url, gas_id, STATUS_READY)
        update_device_status(api_base_url, gas_id, next_status)
