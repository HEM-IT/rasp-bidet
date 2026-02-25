/**
 * MQTT Subscriber (Raspberry Pi 배포 대상)
 * - 진입점: 이 스크립트 실행 후 MQTT로 커맨드 수신
 * - payload 의 simulation / test 여부로 Sim 모드 결정 (또는 env GPIO_SIMULATION)
 *

 * 기기 번호: .env 의 DEVICE_ID 사용. 없으면 기본값(FFFFF).
 * 기동 시 DeviceAP API POST 호출 (gasId 로 기기 등록).
 *
 * 환경변수:
 * - MQTT_URL (default mqtt://52.78.222.49:1883)
 * - DEVICE_ID (default FFFFF)

 * - GPIO_SIMULATION=1 시 테스트 모드 (TEST_GAS_ID, TEST_TEST_ID, TEST_PROFILE_ID 로 payload 보강)
 * - GPIO_CONTROLLER_MAIN (default: 프로젝트 루트의 gpio_controller/main.py 경로)
 * - STATUS_API_URL 또는 API_BASE_URL / DATA_API_URL (디바이스 상태 보고용 API 베이스)
 */
require('dotenv').config();
const mqtt = require('mqtt');
const { spawn } = require('child_process');
const path = require('path');

const MQTT_URL = process.env.MQTT_URL || 'mqtt://52.78.222.49:1883';

// 기기 번호: .env 의 DEVICE_ID 사용
const DEVICE_ID = (process.env.DEVICE_ID || 'FFFFF').trim().toUpperCase().slice(0, 5) || 'FFFFF';
const CLIENT_ID = process.env.MQTT_CLIENT_ID || `device-${DEVICE_ID}`;

// DeviceAP: 기기 등록 (gasId)
const DEVICE_AP_URL = process.env.DEVICE_AP_URL || 'http://bidet.hem-sensorbot.com/api/v1/DeviceAP';
// 디바이스 상태 API (ready 등록용, publisher 서버와 동일)
const DATA_API_URL = (process.env.DATA_API_URL || process.env.API_BASE_URL || 'http://52.78.222.49:3001').replace(/\/$/, '');
const DEVICE_STATUS_PATH = process.env.DATA_API_DEVICE_STATUS_PATH || '/mqtt/api/v1/device/status';

// 테스트 모드 시 payload 에 없을 때 gpio_controller 로 넘길 예시 값 (데이터 꼬임 방지)
const isTestMode = ['1', 'true', 'yes'].includes((process.env.GPIO_SIMULATION || '').toString().toLowerCase());
const TEST_GAS_ID = (process.env.TEST_GAS_ID || 'FFFFF').trim().toUpperCase();
const TEST_TEST_ID = (process.env.TEST_TEST_ID || '00000').trim();
const TEST_PROFILE_ID = process.env.TEST_PROFILE_ID != null ? parseInt(process.env.TEST_PROFILE_ID, 10) : 14;

// 디바이스 상태 보고 API (루트 .env 의 API_BASE_URL / DATA_API_URL 사용 가능)
const STATUS_API_BASE = (process.env.STATUS_API_URL || process.env.DATA_API_URL || process.env.API_BASE_URL || '').replace(/\/+$/, '');
const STATUS_PATH = '/mqtt/api/v1/device/status';

// gpio_controller 경로
const DEFAULT_GPIO_MAIN = path.join(__dirname, '..', 'gpio_controller', 'main.py');
const GPIO_CONTROLLER_MAIN = process.env.GPIO_CONTROLLER_MAIN || DEFAULT_GPIO_MAIN;

// gpio_controller 실행에 쓸 Python: GPIO_VENV(가상환경 경로) 또는 PYTHON_BIN, 없으면 python3
const PYTHON_BIN = (() => {
  const venv = process.env.GPIO_VENV;
  if (venv) {
    const subdir = process.platform === 'win32' ? 'Scripts' : 'bin';
    return path.join(path.resolve(venv), subdir, process.platform === 'win32' ? 'python.exe' : 'python');
  }
  return process.env.PYTHON_BIN || 'python3';
})();

/** payload 에서 Simulation 여부 판단 (env 보다 payload 우선 반영) */
function isSimulationFromPayload(payloadObj) {
  if (!payloadObj || typeof payloadObj !== 'object') return false;
  const v = payloadObj.simulation ?? payloadObj.test;
  return [true, 1, '1', 'true', 'yes'].includes(v);
}

/** 현재 Sim 모드 여부 (env 또는 payload). payload 는 호출처에서 merge 후 사용 */
function isSimMode(envOnly, payloadObj) {
  if (!envOnly && payloadObj && isSimulationFromPayload(payloadObj)) return true;
  return isTestMode;
}

let currentGpioProcess = null;
let lastMeasurementStartedAt = null;

function ts() {
  return new Date().toISOString();
}
function log(...args) {
  console.log(`[${ts()}]`, ...args);
}
function logErr(...args) {
  console.error(`[${ts()}]`, ...args);
}

/** 기동 시 DeviceAP API에 기기 등록 (POST, body: { gasId: 5자리 기기번호 }) */
async function registerDeviceAP() {
  try {
    const res = await fetch(DEVICE_AP_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ gasId: DEVICE_ID }),
    });
    if (!res.ok) {
      logErr('[SUBSCRIBER] DeviceAP 등록 실패', res.status, await res.text());
      return;
    }
    log('[SCENARIO] 2. DeviceAP API 호출 완료', 'gasId=', DEVICE_ID, '| URL=', DEVICE_AP_URL);
  } catch (e) {
    logErr('[SUBSCRIBER] DeviceAP 등록 오류:', e.message);
  }
}

/** 기동 시 /device/status API로 상태 정보 등록 (POST ready, publisher 서버) */
async function registerDeviceStatus() {
  const url = `${DATA_API_URL}${DEVICE_STATUS_PATH}`;
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ gas_id: DEVICE_ID, status: 'ready' }),
    });
    if (res.ok || res.status === 200) {
      log('[SCENARIO] 3. /device/status API 등록 완료', 'gas_id=', DEVICE_ID, '| URL=', url);
      return;
    }
    const text = await res.text();
    logErr('[SUBSCRIBER] device/status 등록 실패', res.status, text);
  } catch (e) {
    logErr('[SUBSCRIBER] device/status 등록 오류:', e.message, '| URL=', url);
  }
}

function runGpioController(payload) {
  return new Promise((resolve, reject) => {
    const env = {
      ...process.env,
      DEVICE_ID,
      MQTT_PAYLOAD: typeof payload === 'string' ? payload : JSON.stringify(payload || {}),
    };
    if (process.env.GPIO_SIMULATION !== undefined) {
      env.GPIO_SIMULATION = process.env.GPIO_SIMULATION;
    }
    const py = spawn(PYTHON_BIN, [GPIO_CONTROLLER_MAIN], {
      cwd: path.dirname(GPIO_CONTROLLER_MAIN),
      env,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    py.stdout?.on('data', (d) => {
      const s = d.toString();
      stdout += s;
      process.stdout.write(s);
    });
    py.stderr?.on('data', (d) => {
      const s = d.toString();
      stderr += s;
      process.stderr.write(s);
    });
    py.on('close', (code) => {
      if (code !== 0) reject(new Error(`gpio_controller exit ${code}: ${stderr || stdout}`));
      else resolve({ stdout, stderr });
    });
  });
}

function stopGpioController() {
  if (!currentGpioProcess) {
    log('[SUBSCRIBER] command/measure/stop: 실행 중인 gpio_controller 없음');
    return;
  }
  log('[SUBSCRIBER] command/measure/stop: gpio_controller 종료 시도');
  currentGpioProcess.kill('SIGTERM');
  currentGpioProcess = null;
}

/**
 * 현재 디바이스 상태를 API로 전송 (웹에서 3~5초 간격 command/status 수신 시 호출)
 */
function reportStatusToApi(payloadObj) {
  return new Promise((resolve) => {
    if (!STATUS_API_BASE) {
      log('[SUBSCRIBER] STATUS_API_URL/API_BASE_URL 없음, 상태 보고 생략');
      return resolve();
    }
    const body = JSON.stringify({
      device_id: DEVICE_ID,
      measuring: !!currentGpioProcess,
      last_measurement_started_at: lastMeasurementStartedAt || null,
      timestamp: new Date().toISOString(),
      ...(payloadObj && typeof payloadObj === 'object' ? payloadObj : {}),
    });
    const url = new URL(STATUS_PATH, STATUS_API_BASE);
    const isHttps = url.protocol === 'https:';
    const req = (isHttps ? https : http).request(
      url.toString(),
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body, 'utf8') },
      },
      (res) => {
        let data = '';
        res.on('data', (ch) => (data += ch));
        res.on('end', () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            log('[SUBSCRIBER] 상태 보고 완료', res.statusCode);
          } else {
            logErr('[SUBSCRIBER] 상태 보고 실패', res.statusCode, data);
          }
          resolve();
        });
      }
    );
    req.on('error', (err) => {
      logErr('[SUBSCRIBER] 상태 API 요청 오류', err.message);
      resolve();
    });
    req.setTimeout(10000, () => {
      req.destroy();
      resolve();
    });
    req.end(body);
  });
}

const client = mqtt.connect(MQTT_URL, {
  clientId: CLIENT_ID,
  clean: true,
  reconnectPeriod: 3000,
});

const prefix = `device/${DEVICE_ID}/`;

client.on('connect', async () => {
  log('[SCENARIO] 1. start.sh로 subscriber 기동 → MQTT 연결됨', 'clientId=', CLIENT_ID, 'deviceId=', DEVICE_ID);
  log('[SUBSCRIBER] 시뮬레이션 모드=', isTestMode ? 'ON (프로덕션은 gas_id/test_id만 전달; 누락 시 테스트값 보강)' : 'OFF (프로덕션)');
  log('[SUBSCRIBER] gpio_controller Python:', PYTHON_BIN);

  await registerDeviceAP();
  await registerDeviceStatus();

  client.subscribe(prefix + '#', { qos: 1 }, (err, granted) => {
    if (err) {
      logErr('[SUBSCRIBER] 구독 실패', err.message);
      return;
    }
    log('[SUBSCRIBER] 구독:', granted.map((g) => g.topic).join(', '));
    log('[SCENARIO] 4. measurement/start(또는 command/measure/start) 수신 대기 중...');
  });
});

/** measurement/start payload 에 profile_id, gas_id, test_id 가 없을 때 테스트 모드면 예시 값으로 채움 */
function ensureTestPayload(payloadObj, simMode) {
  if (!simMode) return payloadObj;
  const out = { ...payloadObj };
  if (out.gas_id == null) out.gas_id = TEST_GAS_ID;
  if (out.test_id == null) out.test_id = TEST_TEST_ID;
  if (out.profile_id == null) out.profile_id = TEST_PROFILE_ID;
  return out;
}

function handleMeasureStart(payloadObj) {
  const simMode = isSimMode(isTestMode, payloadObj);
  const toSend = ensureTestPayload(payloadObj, simMode);
  log('[SUBSCRIBER] measure/start 수신 → gpio_controller 실행', simMode ? '(Sim 모드, 로그 출력)' : '');
  lastMeasurementStartedAt = new Date().toISOString();
  runGpioController(toSend, { background: true, simMode }).catch((e) => {
    logErr('[SUBSCRIBER] gpio_controller 오류:', e.message);
  });
}

client.on('message', async (topic, payload) => {
  const relative = topic.startsWith(prefix) ? topic.slice(prefix.length) : topic;
  let payloadObj = {};
  try {
    const s = payload.toString();
    if (s) payloadObj = JSON.parse(s);
  } catch (_) {}

  const isStartCommand =
    relative === 'measurement/start' ||
    relative === 'command/measure/start' ||
    relative === 'command/measurement/start';
  if (isStartCommand) {
    const simMode = isSimMode(isTestMode, payloadObj);
    const toSend = ensureTestPayload(payloadObj, simMode);
    log('[SCENARIO] 5. 명령 수신 → gpio_controller 실행', 'topic=', relative, simMode ? '(시뮬레이션 보강)' : '');
    try {
      await runGpioController(toSend);
      log('[SCENARIO] 7. gpio_controller 완료 (gas/camera 데이터 API 전달 완료)');
      log('[SCENARIO] 9. 다시 measurement/start 수신 대기 중...');
    } catch (e) {
      logErr('[SUBSCRIBER] gpio_controller 오류:', e.message);
      log('[SCENARIO] 9. 다시 measurement/start 수신 대기 중...');
    }

    return;
  }

  log('[SUBSCRIBER] 메시지 수신', topic, payloadObj);
});

client.on('reconnect', () => log('[SUBSCRIBER] 재연결 중...'));
client.on('close', () => log('[SUBSCRIBER] 연결 종료'));
client.on('error', (err) => logErr('[SUBSCRIBER] 오류', err && (err.message || err.code || err)));

process.on('SIGINT', () => {
  log('[SUBSCRIBER] 종료 중...');
  client.end(true, () => process.exit(0));
});
