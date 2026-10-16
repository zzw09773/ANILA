const PREFIX = 'anila:quick-builder:test:';
const positive = v => Number.isSafeInteger(v) && v > 0;
const uuid = v => typeof v === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(v);
export function newTestRequestId() {
  return globalThis.crypto.randomUUID();
}
export function testStorage() {
  try { return window.sessionStorage; } catch { return null; }
}
export function testSessionKey(actor, assistant) {
  return positive(actor) && positive(assistant) ? `${PREFIX}${actor}:${assistant}` : null;
}
function pointer(value) {
  if (!value || !testSessionKey(value.actor, value.assistant) || !positive(value.version) || !['waiting','unknown','finished'].includes(value.state)) return null;
  if (value.state === 'finished' && !positive(value.run) || value.request_id !== undefined && !uuid(value.request_id)) return null;
  return {actor:value.actor,assistant:value.assistant,version:value.version,state:value.state,
    ...(positive(value.run) ? {run:value.run} : {}),
    ...(uuid(value.request_id) ? {request_id:value.request_id.toLowerCase()} : {})};
}
export function readTestSession(storage, actor, assistant) {
  const key = testSessionKey(actor, assistant);
  if (!key) return null;
  try {
    const value = pointer(JSON.parse(storage.getItem(key)));
    return value?.actor === actor && value.assistant === assistant ? value : null;
  } catch {return null;}
}
export function saveTestSession(storage, value) {
  const safe = pointer(value);
  if (!safe) return false;
  try {
    storage.setItem(testSessionKey(safe.actor, safe.assistant), JSON.stringify(safe));
    return true;
  } catch {return false;}
}
