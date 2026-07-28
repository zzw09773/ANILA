// W2-8 profile 2 — RAG chat against an already-ingested collection.
//
// Same streaming chat path as profile 1, plus the `anila_retrieval` extension,
// which pulls in the piece W2-1 targets: embed the query, vector-search the
// collection, then resolve per-document clearance before any chunk is allowed
// into the prompt.
//
// The collection must already be ingested (see seed-collection.sh); this
// profile deliberately does not ingest, so retrieval cost is measured on its
// own rather than mixed with indexing.
//
//   ANILA_COLLECTION_ID=<id> docker run --rm -i --network host ... \
//     grafana/k6 run /scripts/profile2-rag-chat.js

import { Trend, Rate } from 'k6/metrics';
import http from 'k6/http';
import { check, fail } from 'k6';
import {
  BASE, CHAT_MODEL, baseOptions, login, authHeaders, checkSSE, pickPrompt,
} from './lib/common.js';

const ttft = new Trend('anila_ttft_ms', true);
const complete = new Trend('anila_stream_complete_ms', true);
const failures = new Rate('anila_failed_streams');
// Counts streams whose retrieval stage returned nothing — a 200 with an empty
// context is a silent RAG failure and must not be scored as a success.
const emptyRetrieval = new Rate('anila_empty_retrieval');
// Task creation is part of every RAG query (see the note in the default fn),
// so it is timed separately rather than hidden inside the profile's total.
const taskCreate = new Trend('anila_task_create_ms', true);

const COLLECTION_ID = parseInt(__ENV.ANILA_COLLECTION_ID || '0', 10);
const TOP_K = parseInt(__ENV.ANILA_TOP_K || '5', 10);

export const options = baseOptions();

export function setup() {
  if (!COLLECTION_ID) {
    fail('ANILA_COLLECTION_ID must point at an ingested collection');
  }
  const token = login();
  // Fail fast if the collection is empty: an unseeded collection would make
  // this profile silently degenerate into profile 1.
  const res = http.get(
    `${BASE}/api/ingestion/collections/${COLLECTION_ID}/documents`,
    { headers: authHeaders(token) }
  );
  if (res.status !== 200) {
    fail(`cannot read collection ${COLLECTION_ID}: status=${res.status}`);
  }
  // Terminal success state is 'indexed' (processing_stage 'complete').
  const docs = res.json();
  const ready = (docs || []).filter((d) => d.status === 'indexed');
  if (ready.length === 0) {
    fail(`collection ${COLLECTION_ID} has no indexed documents`);
  }
  return { token: token, docs: ready.length };
}

export default function (data) {
  // A Task seals exactly one retrieval snapshot ever — a second RAG call on
  // the same task_id is refused with 409 snapshot_already_sealed. So one task
  // per query is not test scaffolding, it is how the platform is designed to
  // be driven, and its cost belongs inside the measured RAG path.
  const taskRes = http.post(
    `${BASE}/api/tasks`,
    JSON.stringify({
      title: `loadtest-rag-${__VU}-${__ITER}`,
      task_type: 'query',
      source_scope: 'personal',
      selected_collection_ids: [COLLECTION_ID],
      classification_level: '無機密',
    }),
    { headers: authHeaders(data.token), tags: { profile: 'rag-task' } }
  );
  const taskOk = check(taskRes, {
    'rag: task created (201)': (r) => r.status === 201,
  });
  if (!taskOk) {
    failures.add(true);
    return;
  }
  taskCreate.add(taskRes.timings.duration);
  const taskId = taskRes.json().id;

  const res = http.post(
    `${BASE}/v1/chat/completions`,
    JSON.stringify({
      model: CHAT_MODEL,
      messages: [{ role: 'user', content: pickPrompt() }],
      max_tokens: 128,
      temperature: 0,
      stream: true,
      anila_retrieval: {
        collection_id: COLLECTION_ID,
        top_k: TOP_K,
        min_score: 0.0,
      },
    }),
    {
      headers: authHeaders(data.token, {
        Accept: 'text/event-stream',
        'X-ANILA-Task-Id': String(taskId),
      }),
      timeout: '300s',
      tags: { profile: 'rag-chat' },
    }
  );

  const ok = checkSSE(res, 'rag');
  failures.add(!ok);
  if (ok) {
    ttft.add(res.timings.waiting);
    complete.add(res.timings.duration);
    // CSP emits the sealed citation set as an `event: anila.retrieval` frame
    // ahead of the completion. No such frame means retrieval contributed
    // nothing and the "RAG" answer was really a plain chat answer.
    emptyRetrieval.add((res.body || '').indexOf('anila.retrieval') === -1);
  }
}
