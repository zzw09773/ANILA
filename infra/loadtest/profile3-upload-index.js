// W2-8 profile 3 — document upload + indexing.
//
// Two scenarios run together on purpose:
//
//   uploaders — POST multipart documents into a dedicated collection and time
//               how long CSP takes to accept each one (202).
//   canary    — a cheap authenticated GET issued at a fixed 2 rps throughout.
//
// The canary is the point of this profile. Upload handling that runs
// synchronously on the event loop does not primarily show up as a slow upload;
// it shows up as *everything else* stalling behind it. Comparing
// anila_canary_ms here against the same endpoint measured on an idle stack is
// what makes that stall visible, which is exactly the regression W2-2 addresses.
//
//   docker run --rm -i --network host \
//     -e ANILA_BASE_URL -e ANILA_USER -e ANILA_PASSWORD \
//     -e K6_VUS -e K6_DURATION \
//     -v "$PWD/infra/loadtest:/scripts" grafana/k6 run /scripts/profile3-upload-index.js

import { Trend, Rate, Counter } from 'k6/metrics';
import http from 'k6/http';
import { check, fail, sleep } from 'k6';
import { BASE, VUS, DURATION, login, authHeaders } from './lib/common.js';

// Seconds a VU waits after each upload. Uploads are accepted asynchronously
// (202 + queue), so an unpaced VU can enqueue ~75 docs/s while the worker
// drains only a few per second — the run then measures nothing but how fast
// Postgres accepts rows, and leaves a backlog that poisons every later run.
// Pacing keeps arrival in the same order of magnitude as service rate.
const PACE = parseFloat(__ENV.ANILA_UPLOAD_PACE || '1.0');

const uploadAccept = new Trend('anila_upload_accept_ms', true);
const canary = new Trend('anila_canary_ms', true);
const uploadFail = new Rate('anila_failed_uploads');
const accepted = new Counter('anila_documents_accepted');

// Roughly 40 KB of distinct Chinese prose per document — large enough to make
// parsing and chunking real work, small enough that a run is not dominated by
// network transfer from the k6 container.
const SECTIONS = parseInt(__ENV.ANILA_DOC_SECTIONS || '60', 10);

export const options = {
  scenarios: {
    uploaders: {
      executor: 'constant-vus',
      vus: VUS,
      duration: DURATION,
      exec: 'upload',
      gracefulStop: '120s',
    },
    canary: {
      executor: 'constant-arrival-rate',
      rate: 2,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: 4,
      exec: 'probe',
      gracefulStop: '30s',
    },
  },
  thresholds: {},
};

export function setup() {
  const token = login();

  const me = http.get(`${BASE}/api/auth/me`, { headers: authHeaders(token) });
  if (me.status !== 200) fail(`/api/auth/me failed: ${me.status}`);
  const userId = me.json().id;

  const coll = http.post(
    `${BASE}/api/ingestion/collections`,
    JSON.stringify({
      name: `loadtest-upload-${Date.now()}`,
      description: 'W2-8 upload/index profile',
      chunking_config: { strategy: 'fixed', params: { chunk_size: 600, chunk_overlap: 80 } },
      embedding_model: __ENV.ANILA_EMBED_MODEL || 'nv-embed-v2',
    }),
    { headers: authHeaders(token) }
  );
  if (coll.status !== 201) fail(`collection create failed: ${coll.status} ${coll.body}`);
  const collectionId = coll.json().id;

  // Without a clearance grant + collection access the worker rejects every
  // document after the API has already answered 202 — the upload would look
  // successful while nothing ever indexes.
  const from = new Date(Date.now() - 5 * 60 * 1000).toISOString();
  const until = new Date(Date.now() + 24 * 3600 * 1000).toISOString();
  const grant = http.post(
    `${BASE}/api/clearance/grants`,
    JSON.stringify({
      subject_user_id: userId,
      max_classification_level: '無機密',
      valid_from: from,
      expires_at: until,
      basis_ticket: 'W2-8-loadtest',
    }),
    { headers: authHeaders(token) }
  );
  if (grant.status !== 201) fail(`clearance grant failed: ${grant.status} ${grant.body}`);

  const access = http.post(
    `${BASE}/api/clearance/grants/${grant.json().id}/collections/${collectionId}`,
    JSON.stringify({ membership_granted: true, need_to_know: true, basis_ticket: 'W2-8-loadtest' }),
    { headers: authHeaders(token) }
  );
  if (access.status !== 201) fail(`collection access failed: ${access.status} ${access.body}`);

  return { token: token, collectionId: collectionId };
}

function makeDoc(tag) {
  const parts = [`# 負載測試文件 ${tag}\n`];
  for (let s = 1; s <= SECTIONS; s++) {
    parts.push(
      `## ${tag} 第 ${s} 節\n` +
        `本節描述負載測試文件 ${tag} 的第 ${s} 個段落。平台以向量檢索找出相關內容，` +
        `並在回應前重新評估使用者的資料授權。快取可降低重複查詢延遲，` +
        `稽核紀錄保存每次存取的來源與時間。本節設定值為 ${s * 7}，逾時為 ${30 + s} 秒。\n`
    );
  }
  return parts.join('\n');
}

export function upload(data) {
  // Unique per VU+iteration: documents are keyed by (collection_id, sha256),
  // so repeated identical bodies would be rejected as duplicates rather than
  // exercising the ingest path.
  const tag = `${__VU}-${__ITER}-${Date.now()}`;
  const res = http.post(
    `${BASE}/api/ingestion/collections/${data.collectionId}/documents`,
    { file: http.file(makeDoc(tag), `loadtest-${tag}.md`, 'text/markdown') },
    {
      headers: { Authorization: `Bearer ${data.token}` },
      timeout: '300s',
      tags: { profile: 'upload' },
    }
  );

  const ok = check(res, {
    'upload: status 202': (r) => r.status === 202,
    'upload: json body with document id': (r) => {
      const ct = String(r.headers['Content-Type'] || '');
      if (ct.indexOf('application/json') === -1) return false;
      try { return !!r.json().id; } catch (e) { return false; }
    },
  });
  uploadFail.add(!ok);
  if (ok) {
    uploadAccept.add(res.timings.duration);
    accepted.add(1);
  }
  if (PACE > 0) sleep(PACE);
}

export function probe(data) {
  // Deliberately one of the cheapest authenticated reads available, so its
  // latency reflects event-loop availability rather than query cost.
  const res = http.get(`${BASE}/api/ingestion/collections`, {
    headers: authHeaders(data.token),
    timeout: '120s',
    tags: { profile: 'canary' },
  });
  check(res, { 'canary: status 200': (r) => r.status === 200 });
  canary.add(res.timings.duration);
}
