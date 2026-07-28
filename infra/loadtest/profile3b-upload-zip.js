// W2-8 profile 3b — ZIP upload + indexing (the W2-2 code path).
//
// Why this exists separately from profile 3: W2-2 is specifically about
// `upload_zip` (POST /api/ingestion/collections/{id}/documents/zip), whose
// per-member loop — read, validate, sha256, persist, for up to 1 GB — runs
// inside an `async def`. Profile 3 posts single documents and therefore never
// touches that loop, so it cannot show the regression W2-2 fixes.
//
// The measurement that matters here is `anila_canary_ms`, NOT the upload time.
// A synchronous per-member loop stalls the whole event loop, so the symptom is
// that unrelated cheap requests freeze while a zip is being ingested. Compare
// the canary p95 here against profile 3's canary at the same VU level.
//
// Requires fixtures: ./make-zip-fixtures.sh 40 60
//
//   ANILA_VUS=4 ANILA_DURATION=40s docker run ... \
//     grafana/k6 run /scripts/profile3b-upload-zip.js

import { Trend, Rate, Counter } from 'k6/metrics';
import http from 'k6/http';
import { check, fail } from 'k6';
import { BASE, VUS, DURATION, login, authHeaders } from './lib/common.js';

const zipAccept = new Trend('anila_zip_accept_ms', true);
const canary = new Trend('anila_canary_ms', true);
const zipFail = new Rate('anila_failed_uploads');
const accepted = new Counter('anila_zips_accepted');

const ZIP_COUNT = parseInt(__ENV.ANILA_ZIP_COUNT || '40', 10);

// open() is init-context only. Each zip is distinct because documents are keyed
// by (collection_id, sha256) — re-posting one zip would just be rejected as
// duplicates instead of exercising the ingest path.
const ZIPS = [];
for (let i = 1; i <= ZIP_COUNT; i++) {
  ZIPS.push(open(`./fixtures/bundle-${i}.zip`, 'b'));
}

export const options = {
  scenarios: {
    zippers: {
      executor: 'constant-vus',
      vus: VUS,
      duration: DURATION,
      exec: 'zipUpload',
      gracefulStop: '180s',
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
  setupTimeout: '180s',
};

export function setup() {
  const token = login();

  const me = http.get(`${BASE}/api/auth/me`, { headers: authHeaders(token) });
  if (me.status !== 200) fail(`/api/auth/me failed: ${me.status}`);

  const coll = http.post(
    `${BASE}/api/ingestion/collections`,
    JSON.stringify({
      name: `loadtest-zip-${Date.now()}`,
      description: 'W2-8 zip upload profile (W2-2 path)',
      chunking_config: { strategy: 'fixed', params: { chunk_size: 600, chunk_overlap: 80 } },
      embedding_model: __ENV.ANILA_EMBED_MODEL || 'nv-embed-v2',
    }),
    { headers: authHeaders(token) }
  );
  if (coll.status !== 201) fail(`collection create failed: ${coll.status} ${coll.body}`);
  const collectionId = coll.json().id;

  const from = new Date(Date.now() - 5 * 60 * 1000).toISOString();
  const until = new Date(Date.now() + 24 * 3600 * 1000).toISOString();
  const grant = http.post(
    `${BASE}/api/clearance/grants`,
    JSON.stringify({
      subject_user_id: me.json().id,
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

export function zipUpload(data) {
  // Each VU walks its own slice of the fixture set so two VUs never post the
  // same archive into the same collection.
  const idx = (__VU - 1 + __ITER * VUS) % ZIP_COUNT;
  const res = http.post(
    `${BASE}/api/ingestion/collections/${data.collectionId}/documents/zip`,
    { file: http.file(ZIPS[idx], `bundle-${idx + 1}.zip`, 'application/zip') },
    {
      headers: { Authorization: `Bearer ${data.token}` },
      timeout: '600s',
      tags: { profile: 'zip-upload' },
    }
  );

  const ok = check(res, {
    'zip: status 202': (r) => r.status === 202,
    'zip: json body': (r) =>
      String(r.headers['Content-Type'] || '').indexOf('application/json') !== -1,
  });
  zipFail.add(!ok);
  if (ok) {
    zipAccept.add(res.timings.duration);
    accepted.add(1);
  }
}

export function probe(data) {
  const res = http.get(`${BASE}/api/ingestion/collections`, {
    headers: authHeaders(data.token),
    timeout: '120s',
    tags: { profile: 'canary' },
  });
  check(res, { 'canary: status 200': (r) => r.status === 200 });
  canary.add(res.timings.duration);
}
