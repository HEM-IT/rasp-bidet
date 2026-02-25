/**
 * EC2 브로커 연결 + 메시지 수신만 확인하는 테스트 (gpio_controller 미호출)
 * 사용: MQTT_URL=mqtt://<EC2_IP>:1883 DEVICE_ID=FFFFF node mqtt_subscriber/test-receive-only.js
 */
require('dotenv').config();
const mqtt = require('mqtt');

const MQTT_URL = process.env.MQTT_URL || 'mqtt://52.78.222.49:1883';
const DEVICE_ID = (process.env.DEVICE_ID || 'FFFFF').trim().toUpperCase();
const CLIENT_ID = process.env.MQTT_CLIENT_ID || `device-${DEVICE_ID}-test`;

const prefix = `device/${DEVICE_ID}/`;

function ts() {
  return new Date().toISOString();
}

const client = mqtt.connect(MQTT_URL, {
  clientId: CLIENT_ID,
  clean: true,
  reconnectPeriod: 3000,
});

client.on('connect', () => {
  console.log(`[${ts()}] [TEST] MQTT 연결됨 clientId=${CLIENT_ID} deviceId=${DEVICE_ID}`);
  client.subscribe(prefix + '#', { qos: 1 }, (err, granted) => {
    if (err) {
      console.error(`[${ts()}] [TEST] 구독 실패`, err.message);
      return;
    }
    console.log(`[${ts()}] [TEST] 구독 완료:`, granted.map((g) => g.topic).join(', '));
    console.log(`[${ts()}] [TEST] 이제 EC2 publisher API로 measurement/start 트리거하면 여기서 수신 로그가 찍힙니다.`);
  });
});

client.on('message', (topic, payload) => {
  const relative = topic.startsWith(prefix) ? topic.slice(prefix.length) : topic;
  let body = {};
  try {
    const s = payload.toString();
    if (s) body = JSON.parse(s);
  } catch (_) {}
  console.log(`[${ts()}] [TEST] 수신 topic=${topic} relative=${relative} payload=`, JSON.stringify(body));
});

client.on('reconnect', () => console.log(`[${ts()}] [TEST] 재연결 중...`));
client.on('close', () => console.log(`[${ts()}] [TEST] 연결 종료`));
client.on('error', (err) => console.error(`[${ts()}] [TEST] 오류`, err && (err.message || err.code || err)));

process.on('SIGINT', () => {
  console.log(`[${ts()}] [TEST] 종료`);
  client.end(true, () => process.exit(0));
});
