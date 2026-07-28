// W2-8 control — drive the upstream model gateway DIRECTLY, bypassing ANILA.
//
// This is not one of the three required profiles; it exists so the other three
// can be interpreted honestly. Profiles 1 and 2 send every token through an
// external OpenAI-compatible gateway, so a latency knee could belong either to
// ANILA or to the gateway. Running the identical request shape straight at the
// gateway at the same VU levels separates the two:
//
//   knee here AND in profile 1   -> the gateway is the limit; ANILA's own
//                                   ceiling is not what was measured
//   knee only in profile 1       -> the limit is ANILA's
//
// The bearer token is read from the environment and never stored in this file.
//
//   ANILA_GATEWAY_URL=<base, no /v1> ANILA_GATEWAY_KEY=<token> \
//     docker run ... grafana/k6 run /scripts/control-gateway-direct.js

import { Trend, Rate } from 'k6/metrics';
import http from 'k6/http';
import { check, fail } from 'k6';
import { baseOptions, pickPrompt, isSSE, sseLooksComplete } from './lib/common.js';

const ttft = new Trend('anila_ttft_ms', true);
const complete = new Trend('anila_stream_complete_ms', true);
const failures = new Rate('anila_failed_streams');

const GW = __ENV.ANILA_GATEWAY_URL;
const KEY = __ENV.ANILA_GATEWAY_KEY;
const MODEL = __ENV.ANILA_CHAT_MODEL || 'gemma26-nothink';

export const options = baseOptions();

export function setup() {
  if (!GW || !KEY) fail('ANILA_GATEWAY_URL and ANILA_GATEWAY_KEY must be set');
  return {};
}

export default function () {
  const res = http.post(
    `${GW}/v1/chat/completions`,
    JSON.stringify({
      model: MODEL,
      messages: [{ role: 'user', content: pickPrompt() }],
      // Must match profiles 1 and 2 exactly or the comparison is meaningless.
      max_tokens: 128,
      temperature: 0,
      stream: true,
    }),
    {
      headers: {
        Authorization: `Bearer ${KEY}`,
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
      },
      timeout: '300s',
      tags: { profile: 'gateway-direct' },
    }
  );

  const ok = check(res, {
    'gw: status 200': (r) => r.status === 200,
    'gw: content-type is text/event-stream': (r) => isSSE(r),
    'gw: stream terminated with [DONE]': (r) => sseLooksComplete(r),
  });
  failures.add(!ok);
  if (ok) {
    ttft.add(res.timings.waiting);
    complete.add(res.timings.duration);
  }
}
