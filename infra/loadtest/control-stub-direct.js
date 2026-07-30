// Control: hit the embed stub directly (bypass ANILA).
// Separates stub latency from platform overhead.
//
//   EMBED_STUB_URL=http://127.0.0.1:18088 ANILA_VUS=8 \
//     docker run --rm -i --network host -v $PWD/infra/loadtest:/scripts:ro \
//       -e EMBED_STUB_URL -e ANILA_VUS -e ANILA_DURATION \
//       grafana/k6 run /scripts/control-stub-direct.js

import http from 'k6/http';
import { check } from 'k6';
import { Trend, Rate } from 'k6/metrics';
import { baseOptions } from './lib/common.js';

const BASE = __ENV.EMBED_STUB_URL || 'http://127.0.0.1:18088';
const latency = new Trend('stub_embed_ms', true);
const failures = new Rate('stub_failed');

export const options = baseOptions();

export default function () {
  const res = http.post(
    `${BASE}/v1/embeddings`,
    JSON.stringify({ model: 'loadtest-embed', input: `q-${__VU}-${__ITER}` }),
    { headers: { 'Content-Type': 'application/json' }, timeout: '60s' }
  );
  const ok = check(res, {
    'stub 200': (r) => r.status === 200,
    'has embedding': (r) => {
      try { return (r.json().data || [])[0].embedding.length > 0; } catch (e) { return false; }
    },
  });
  failures.add(!ok);
  if (ok) latency.add(res.timings.duration);
}

export function handleSummary(data) {
  const m = data.metrics || {};
  const v = (n, s) => (m[n] && m[n].values ? m[n].values[s] : null);
  return {
    stdout: [
      `vus=${__ENV.ANILA_VUS || '?'}`,
      `stub_p50_ms=${v('stub_embed_ms', 'p(50)')}`,
      `stub_p95_ms=${v('stub_embed_ms', 'p(95)')}`,
      `stub_fail_rate=${v('stub_failed', 'rate')}`,
      '',
    ].join('\n'),
  };
}
