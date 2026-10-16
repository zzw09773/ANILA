const PREFIX = 'anila:quick-builder:test:';
const positive = v => Number.isSafeInteger(v) && v > 0;
export function testStorage() {
  try { return window.sessionStorage; } catch { return null; }
}
export function testSessionKey(actor, assistant) {
  return positive(actor) && positive(assistant) ? `${PREFIX}${actor}:${assistant}` : null;
}
export function readTestSession(storage, actor, assistant) {
  const key = testSessionKey(actor, assistant);
  if (!key) return null;
  try {
    const v = JSON.parse(storage.getItem(key));
    if (v?.actor !== actor || v.assistant !== assistant || !positive(v.version) || !['waiting','unknown','finished'].includes(v.state)) return null;
    if (v.state === 'finished' && !positive(v.run)) return null;
    return {actor,assistant,version:v.version,state:v.state, ...(v.state === 'finished' ? {run:v.run} : {})};
  } catch {return null;}
}
export function saveTestSession(storage, value) {
  const key = testSessionKey(value.actor, value.assistant);
  if (!key || !positive(value.version) || !['waiting','unknown','finished'].includes(value.state) || value.state === 'finished' && !positive(value.run)) return false;
  try {
    storage.setItem(key, JSON.stringify({actor:value.actor,assistant:value.assistant,version:value.version,state:value.state,...(value.state === 'finished' ? {run:value.run} : {})}));
    return true;
  } catch {return false;}
}
