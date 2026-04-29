// Cache of `enabled-actions` for the ChatRuntime toolbar.
//
// Plain module-state cache + invalidation API. Any time the user
// creates / saves / status-changes a Function, call `invalidate()` so
// the next render fetches fresh data. Otherwise the toolbar stays
// stable across re-renders (no per-message re-fetch).

import { listEnabledActions } from "./functions.js";

let cached = null;
let inflight = null;

export function getCachedEnabledActions() {
  return cached;
}

export async function loadEnabledActions(...auth) {
  if (cached !== null) return cached;
  if (inflight) return inflight;
  inflight = (async () => {
    try {
      const data = await listEnabledActions(...auth);
      cached = data?.actions || [];
      return cached;
    } finally {
      inflight = null;
    }
  })();
  return inflight;
}

export function invalidate() {
  cached = null;
  inflight = null;
}
