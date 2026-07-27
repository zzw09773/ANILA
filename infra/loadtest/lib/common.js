// Shared helpers for the ANILA k6 load profiles (W2-8).
//
// Every profile authenticates the way a real browser client does:
//   POST /api/auth/login  ->  access_token  ->  Authorization: Bearer ...
// No API keys or bearer tokens are baked into these files; everything comes
// from environment variables supplied at run time.

import http from 'k6/http';
import { check, fail } from 'k6';

export const BASE = __ENV.ANILA_BASE_URL || 'http://127.0.0.1:18000';
export const USERNAME = __ENV.ANILA_USER || 'admin';
export const PASSWORD = __ENV.ANILA_PASSWORD;
export const CHAT_MODEL = __ENV.ANILA_CHAT_MODEL || 'gemma26-nothink';

// One VU level per run — the sweep driver (run-sweep.sh) invokes the script
// once per level so each level yields an uncontaminated p95.
//
// ⚠️ These MUST NOT be called K6_VUS / K6_DURATION. Those names are k6's own
// reserved env-level options, and setting them makes k6 discard the entire
// `scenarios` block ("env level configuration overrode scenarios configuration
// entirely"). For profile 3 that would silently delete the canary scenario and
// leave the profile measuring nothing but uploads.
export const VUS = parseInt(__ENV.ANILA_VUS || '1', 10);
export const DURATION = __ENV.ANILA_DURATION || '60s';

export function baseOptions(extra) {
  return Object.assign(
    {
      scenarios: {
        level: {
          executor: 'constant-vus',
          vus: VUS,
          duration: DURATION,
          gracefulStop: '120s',
        },
      },
      // No pass/fail gate: W2-8 records numbers, it does not set a bar.
      // (A capacity bar is Gate 6's job, not this package's.)
      thresholds: {},
      discardResponseBodies: false,
      // setup() only logs in and reads one collection, but it runs against a
      // stack that may still be draining the previous sweep level. k6's 60s
      // default aborts the whole run with 0 iterations when that happens,
      // which looks identical to "the platform collapsed" in the results.
      // Give it room, and let run-sweep.sh cool down between levels.
      setupTimeout: '180s',
    },
    extra || {}
  );
}

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

export function login() {
  if (!PASSWORD) {
    fail('ANILA_PASSWORD must be set (never hard-code it in the script)');
  }
  const res = http.post(
    `${BASE}/api/auth/login`,
    JSON.stringify({ username: USERNAME, password: PASSWORD }),
    { headers: { 'Content-Type': 'application/json' }, tags: { op: 'login' } }
  );
  // Validate by Content-Type AND body shape: the SPA catch-all answers 200
  // text/html for unmatched routes, so a bare status check proves nothing.
  const ct = String(res.headers['Content-Type'] || '');
  if (res.status !== 200 || ct.indexOf('application/json') === -1) {
    fail(`login failed: status=${res.status} content-type=${ct}`);
  }
  const body = res.json();
  if (!body || !body.access_token) {
    fail('login response carried no access_token');
  }
  return body.access_token;
}

export function authHeaders(token, extra) {
  return Object.assign(
    { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    extra || {}
  );
}

// ---------------------------------------------------------------------------
// SSE
// ---------------------------------------------------------------------------

// What this actually measures
// ---------------------------
// k6 has no incremental SSE reader, so each streamed completion is issued as a
// normal POST and the response is scored on two timings k6 records natively:
//
//   res.timings.waiting  = time from request write to the FIRST response byte.
//                          For a streamed completion the first byte is the
//                          first `data:` frame, so this is time-to-first-token.
//   res.timings.duration = time until the stream closed, i.e. full completion.
//
// Both are reported per profile. TTFT is the number that reflects perceived
// chat responsiveness; duration is bounded by max_tokens and is mostly a
// throughput signal.
export function isSSE(res) {
  const ct = String(res.headers['Content-Type'] || '');
  return ct.indexOf('text/event-stream') !== -1;
}

// A well-formed streamed completion carries at least one `data:` frame and is
// terminated by `data: [DONE]`. Truncated streams (upstream timeout, worker
// kill) still return 200, so the body shape is the only honest success test.
export function sseLooksComplete(res) {
  const b = res.body || '';
  return b.indexOf('data:') !== -1 && b.indexOf('[DONE]') !== -1;
}

export function checkSSE(res, name) {
  return check(res, {
    [`${name}: status 200`]: (r) => r.status === 200,
    [`${name}: content-type is text/event-stream`]: (r) => isSSE(r),
    [`${name}: stream terminated with [DONE]`]: (r) => sseLooksComplete(r),
  });
}

export const PROMPTS = [
  '請用一句話說明什麼是向量資料庫。',
  '簡短說明 HTTP 與 HTTPS 的差別。',
  '用兩句話介紹什麼是機器學習。',
  '請簡述關聯式資料庫的正規化目的。',
  '一句話說明什麼是快取。',
];

export function pickPrompt() {
  return PROMPTS[Math.floor(Math.random() * PROMPTS.length)];
}
