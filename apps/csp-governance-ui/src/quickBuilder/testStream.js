export const TEST_STREAM_LIMITS = Object.freeze({frameBytes: 1024 * 1024, totalBytes: 16 * 1024 * 1024, events: 2048, lines: 4096});
const EVENTS = new Set(['quick.run_started', 'quick.run_snapshot', 'quick.step_started', 'quick.step_finished', 'quick.output_delta', 'quick.run_finished', 'quick.typed_error']);
const COPIES = {
  TEST_STREAM_INVALID: '無法確認即時進度資料，將查詢同一次試跑。',
  TEST_STREAM_LIMIT: '即時進度超過可接收範圍，將查詢同一次試跑的完整紀錄。',
  TEST_STREAM_INTERRUPTED: '即時進度連線已中斷，尚未確認執行結果，將查詢同一次試跑。',
  TEST_STREAM_CONTEXT: '即時進度的執行識別不符，將重新查詢原本的試跑。',
  TEST_STREAM_SEQUENCE: '即時進度順序無法確認，將查詢同一次試跑。'
};
export function testStreamError(code = 'TEST_STREAM_INVALID') {
  const safeCode = typeof code === 'string' && Object.hasOwn(COPIES, code) ? code : 'TEST_STREAM_INVALID';
  return Object.assign(new Error(`${COPIES[safeCode]}（${safeCode}）`), {code: safeCode});
}
const abortError = () => Object.assign(new Error('已停止等待即時進度。'), {name: 'AbortError', code: 'ERR_CANCELED'});

export async function readTestJsonStream(stream, {signal, maxBytes = 16 * 1024 * 1024} = {}) {
  if (!stream || typeof stream.getReader !== 'function') throw testStreamError();
  const reader = stream.getReader();
  const decoder = new TextDecoder('utf-8', {fatal: true});
  let text = '', bytes = 0;
  try {
    while (true) {
      if (signal?.aborted) throw abortError();
      const chunk = await reader.read();
      if (signal?.aborted) throw abortError();
      if (chunk.done) break;
      bytes += chunk.value.byteLength;
      if (bytes > maxBytes) throw testStreamError('TEST_STREAM_LIMIT');
      text += decoder.decode(chunk.value, {stream: true});
    }
    text += decoder.decode();
    try { return JSON.parse(text); } catch { throw testStreamError(); }
  } finally {
    try { await reader.cancel(); } catch { /* Observer cleanup does not cancel execution. */ }
    reader.releaseLock();
  }
}

// Each awaited callback finishes before another frame is read or applied.
export async function readTestEventStream(stream, {signal, onEvent, limits = {}}) {
  if (!stream || typeof stream.getReader !== 'function' || typeof onEvent !== 'function') throw testStreamError();
  const bound = {...TEST_STREAM_LIMITS, ...limits};
  for (const value of Object.values(bound)) if (!Number.isSafeInteger(value) || value <= 0) throw testStreamError();
  const reader = stream.getReader(), decoder = new TextDecoder('utf-8', {fatal: true}), encoder = new TextEncoder();
  let buffer = '', event = 'message', id = '', data = [], frameBytes = 0, lineCount = 0, totalBytes = 0, count = 0, terminal = false;
  const onAbort = () => { Promise.resolve(reader.cancel()).catch(() => {}); };
  signal?.addEventListener('abort', onAbort, {once: true});
  async function line(value) {
    frameBytes += encoder.encode(value).byteLength + 1;
    if (frameBytes > bound.frameBytes || ++lineCount > bound.lines) throw testStreamError('TEST_STREAM_LIMIT');
    if (!value) {
      if (data.length) {
        if (++count > bound.events) throw testStreamError('TEST_STREAM_LIMIT');
        if (!EVENTS.has(event)) throw testStreamError();
        let payload;
        try { payload = JSON.parse(data.join('\n')); } catch { throw testStreamError(); }
        terminal = await onEvent({event, id, data: payload}) === true;
      }
      event = 'message'; id = ''; data = []; frameBytes = 0; lineCount = 0;
      return;
    }
    if (value.startsWith(':')) return;
    const colon = value.indexOf(':');
    const key = colon < 0 ? value : value.slice(0, colon);
    let content = colon < 0 ? '' : value.slice(colon + 1);
    if (content.startsWith(' ')) content = content.slice(1);
    if (key === 'event') event = content;
    else if (key === 'id') id = content;
    else if (key === 'data') data.push(content);
  }
  try {
    while (!terminal) {
      if (signal?.aborted) throw abortError();
      const chunk = await reader.read();
      if (signal?.aborted) throw abortError();
      if (chunk.done) {
        try { decoder.decode(); } catch { throw testStreamError(); }
        throw testStreamError('TEST_STREAM_INTERRUPTED');
      }
      if (!(chunk.value instanceof Uint8Array)) throw testStreamError();
      totalBytes += chunk.value.byteLength;
      if (totalBytes > bound.totalBytes) throw testStreamError('TEST_STREAM_LIMIT');
      try { buffer += decoder.decode(chunk.value, {stream: true}); } catch { throw testStreamError(); }
      while (!terminal) {
        const end = buffer.search(/[\r\n]/);
        if (end < 0 || buffer[end] === '\r' && end === buffer.length - 1) break;
        const value = buffer.slice(0, end), size = buffer[end] === '\r' && buffer[end + 1] === '\n' ? 2 : 1;
        buffer = buffer.slice(end + size);
        await line(value);
        if (signal?.aborted) throw abortError();
      }
      if (encoder.encode(buffer).byteLength + frameBytes > bound.frameBytes) throw testStreamError('TEST_STREAM_LIMIT');
    }
    return {terminal: true, events: count};
  } finally {
    signal?.removeEventListener('abort', onAbort);
    try { await reader.cancel(); } catch { /* Detach only; server execution is independent. */ }
    reader.releaseLock();
  }
}
