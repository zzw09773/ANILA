// W2-8 profile 1 — pure chat, streamed (SSE).
//
// Exercises the hot path a normal chat user hits: CSP auth + model registry
// lookup + usage metering + the streaming proxy to the upstream model. No
// retrieval, no ingestion.
//
//   docker run --rm -i --network host \
//     -e ANILA_BASE_URL -e ANILA_USER -e ANILA_PASSWORD -e ANILA_CHAT_MODEL \
//     -e K6_VUS -e K6_DURATION \
//     -v "$PWD/infra/loadtest:/scripts" grafana/k6 run /scripts/profile1-chat-sse.js

import { Trend, Rate } from 'k6/metrics';
import http from 'k6/http';
import {
  BASE, CHAT_MODEL, baseOptions, login, authHeaders, checkSSE, pickPrompt,
} from './lib/common.js';

// TTFT is tracked separately from k6's built-in http_req_waiting so the report
// carries one unambiguous "time to first token" series per profile.
const ttft = new Trend('anila_ttft_ms', true);
const complete = new Trend('anila_stream_complete_ms', true);
const failures = new Rate('anila_failed_streams');

export const options = baseOptions();

export function setup() {
  return { token: login() };
}

export default function (data) {
  const res = http.post(
    `${BASE}/v1/chat/completions`,
    JSON.stringify({
      model: CHAT_MODEL,
      messages: [{ role: 'user', content: pickPrompt() }],
      // Capped low and kept constant across VU levels: completion length must
      // not vary between runs or the p95 comparison is meaningless.
      max_tokens: 128,
      temperature: 0,
      stream: true,
    }),
    {
      headers: authHeaders(data.token, { Accept: 'text/event-stream' }),
      timeout: '300s',
      tags: { profile: 'chat-sse' },
    }
  );

  const ok = checkSSE(res, 'chat');
  failures.add(!ok);
  if (ok) {
    ttft.add(res.timings.waiting);
    complete.add(res.timings.duration);
  }
}
