// Profile: concurrent collection search — the path that held a pooled DB
// connection across the outbound embedding HTTP call (pool-of-30 cliff).
//
// Replaces attic profile2-rag-chat.js as the primary probe for tonight's
// pool fix: chat/SSE needs a real LLM and obscures pool behaviour with
// token-generation latency. This hits POST /api/ingestion/collections/{id}/search
// which is exactly `_embed_query` → pgvector ANN.
//
//   ANILA_COLLECTION_ID=<id> ANILA_PASSWORD=... \
//     docker run --rm -i --network host -v "$PWD/infra/loadtest:/scripts:ro" \
//       -e ANILA_PASSWORD -e ANILA_COLLECTION_ID -e ANILA_VUS=8 -e ANILA_DURATION=30s \
//       grafana/k6 run /scripts/profile-search.js

import { Trend, Rate, Counter } from 'k6/metrics';
import http from 'k6/http';
import { check, fail } from 'k6';
import {
  BASE, baseOptions, login, authHeaders, pickPrompt,
} from './lib/common.js';

const latency = new Trend('anila_search_ms', true);
const failures = new Rate('anila_search_failed');
const http5xx = new Counter('anila_http_5xx');
const http429 = new Counter('anila_http_429');
const emptyHits = new Rate('anila_empty_hits');

const COLLECTION_ID = parseInt(__ENV.ANILA_COLLECTION_ID || '0', 10);
const TOP_K = parseInt(__ENV.ANILA_TOP_K || '5', 10);

export const options = baseOptions();

export function setup() {
  if (!COLLECTION_ID) {
    fail('ANILA_COLLECTION_ID must point at an ingested collection');
  }
  const token = login();
  const res = http.get(
    `${BASE}/api/ingestion/collections/${COLLECTION_ID}/documents`,
    { headers: authHeaders(token) }
  );
  if (res.status !== 200) {
    fail(`cannot read collection ${COLLECTION_ID}: status=${res.status}`);
  }
  const docs = res.json();
  const ready = (docs || []).filter((d) => d.status === 'indexed');
  if (ready.length === 0) {
    fail(`collection ${COLLECTION_ID} has no indexed documents`);
  }
  // Warm one search so TLS / JWT / first-embed path is not in the sample.
  const warm = http.post(
    `${BASE}/api/ingestion/collections/${COLLECTION_ID}/search`,
    JSON.stringify({ query: 'warmup', top_k: TOP_K, min_score: 0.0 }),
    { headers: authHeaders(token), timeout: '120s' }
  );
  if (warm.status !== 200) {
    fail(`warmup search failed: status=${warm.status} body=${String(warm.body).slice(0, 300)}`);
  }
  return { token: token, docs: ready.length };
}

export default function (data) {
  const res = http.post(
    `${BASE}/api/ingestion/collections/${COLLECTION_ID}/search`,
    JSON.stringify({
      query: pickPrompt(),
      top_k: TOP_K,
      min_score: 0.0,
    }),
    {
      headers: authHeaders(data.token),
      timeout: '120s',
      tags: { profile: 'search' },
    }
  );

  if (res.status >= 500) http5xx.add(1);
  if (res.status === 429) http429.add(1);

  const ok = check(res, {
    'search: status 200': (r) => r.status === 200,
    'search: json content-type': (r) =>
      String(r.headers['Content-Type'] || '').indexOf('application/json') !== -1,
    'search: has results array': (r) => {
      try {
        const body = r.json();
        return body && Array.isArray(body.results);
      } catch (e) {
        return false;
      }
    },
  });

  failures.add(!ok);
  if (ok) {
    latency.add(res.timings.duration);
    try {
      emptyHits.add((res.json().results || []).length === 0);
    } catch (e) {
      emptyHits.add(true);
    }
  }
}

export function handleSummary(data) {
  return {
    stdout: textSummary(data),
  };
}

function textSummary(data) {
  const m = data.metrics || {};
  const pick = (name, stat) => {
    const v = m[name] && m[name].values ? m[name].values[stat] : undefined;
    return v === undefined ? null : v;
  };
  // k6 Trend summaries expose median as 'med', not 'p(50)'.
  const p50 = pick('anila_search_ms', 'med') ?? pick('anila_search_ms', 'p(50)');
  const lines = [
    `vus=${__ENV.ANILA_VUS || '?'} duration=${__ENV.ANILA_DURATION || '?'}`,
    `search_p50_ms=${p50}`,
    `search_p95_ms=${pick('anila_search_ms', 'p(95)')}`,
    `search_p99_ms=${pick('anila_search_ms', 'p(99)')}`,
    `search_fail_rate=${pick('anila_search_failed', 'rate')}`,
    `http_5xx=${pick('anila_http_5xx', 'count')}`,
    `http_429=${pick('anila_http_429', 'count')}`,
    `checks_rate=${pick('checks', 'rate')}`,
    `iterations=${pick('iterations', 'count')}`,
  ];
  return lines.join('\n') + '\n';
}
