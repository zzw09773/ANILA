// Shared helpers for ANILA k6 load profiles (reclaimed from attic W2-8,
// adapted 2026-07-31 for the redesign stack behind nginx on :443).
//
// Auth matches a real SPA client: POST /api/auth/login → Bearer token.
// No secrets are baked in; everything comes from the environment.

import http from 'k6/http';
import { check, fail } from 'k6';

export const BASE = __ENV.ANILA_BASE_URL || 'https://127.0.0.1';
export const USERNAME = __ENV.ANILA_USER || 'admin';
export const PASSWORD = __ENV.ANILA_PASSWORD;
export const INSECURE = (__ENV.ANILA_INSECURE_TLS || '1') === '1';

export function baseOptions() {
  // One VU level per run — run-sweep.sh invokes the script once per level.
  // Do NOT name these K6_VUS / K6_DURATION (k6 reserved; would discard scenarios).
  const vus = parseInt(__ENV.ANILA_VUS || '1', 10);
  const duration = __ENV.ANILA_DURATION || '30s';
  return {
    scenarios: {
      load: {
        executor: 'constant-vus',
        vus: vus,
        duration: duration,
        gracefulStop: '30s',
      },
    },
    // TLS to local nginx uses the CSPKI leaf; for host runs we skip verify.
    insecureSkipTLSVerify: INSECURE,
    thresholds: {
      // Soft — sweep driver records numbers; we do not fail the process on
      // threshold breach so a cliff stays visible in the summary JSON.
      checks: ['rate>0'],
    },
  };
}

export function login() {
  if (!PASSWORD) {
    fail('ANILA_PASSWORD must be set');
  }
  const res = http.post(
    `${BASE}/api/auth/login`,
    JSON.stringify({ username: USERNAME, password: PASSWORD }),
    {
      headers: { 'Content-Type': 'application/json' },
      tags: { profile: 'login' },
    }
  );
  if (res.status !== 200) {
    fail(`login failed: status=${res.status} body=${String(res.body).slice(0, 200)}`);
  }
  const token = res.json('access_token');
  if (!token) {
    fail('login response missing access_token');
  }
  return token;
}

export function authHeaders(token, extra) {
  const h = Object.assign(
    {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    extra || {}
  );
  return h;
}

export function pickPrompt() {
  const prompts = [
    '平台連線池如何設定？',
    '向量檢索的 top_k 預設是多少？',
    '文件索引失敗時要看哪個狀態欄位？',
    '如何註冊 embedding 模型？',
    'RAG 搜尋會不會跨 collection？',
  ];
  return prompts[(__VU + __ITER) % prompts.length];
}
