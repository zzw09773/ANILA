import { maskTrace } from './graph.js';
import { completionTestResult } from './completion.js';
import { executionErrorNotice } from './executionError.js';
import { testStreamError } from './testStream.js';

const positive = value => Number.isSafeInteger(value) && value > 0;
const TERMINAL = ['completed', 'failed', 'cancelled', 'interrupted'];
const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
const safeError = value => { if (value?.type === 'IDENTITY_CHANGED') return {type:'IDENTITY_CHANGED',message:'登入身分已變更，已停止接收原本的試跑資料（IDENTITY_CHANGED）。'}; const safe = executionErrorNotice(value); return {type: safe.type, message: safe.message}; };

export function createTestStreamState(context, graph) {
  if (!positive(context.version) || !positive(context.revision) || !uuid(context.request_id)) throw testStreamError('TEST_STREAM_CONTEXT');
  const nodes = new Map((graph?.nodes || []).map(node => [node.id, node]));
  let run = context.run || null, seq = 0, opened = false, closed = false, output = '';
  let result = {status: 'running', output: '', steps: [], artifacts: []};
  const started = new Set();
  function step(raw) {
    if (!raw || typeof raw !== 'object' || !nodes.has(raw.node_id) || raw.kind !== nodes.get(raw.node_id).kind) throw testStreamError('TEST_STREAM_CONTEXT');
    return completionTestResult({steps: [raw]}, {steps: [maskTrace(raw)]}).steps[0];
  }
  function upsert(value) {
    const index = result.steps.findIndex(item => item.node_id === value.node_id);
    if (index < 0) result.steps.push(value); else result.steps[index] = value;
  }
  function projectRun(raw) {
    if (!raw || !positive(raw.id) || run && raw.id !== run || raw.version_id !== context.version || raw.request_id !== context.request_id || raw.revision !== undefined && raw.revision !== context.revision || !['running', ...TERMINAL].includes(raw.status) || !Array.isArray(raw.steps) || raw.steps.length > nodes.size || new Set(raw.steps.map(value => value?.node_id)).size !== raw.steps.length) throw testStreamError('TEST_STREAM_CONTEXT');
    run = raw.id;
    const clean = completionTestResult(raw, maskTrace(raw));
    clean.steps = raw.steps.map(step);
    return clean;
  }
  function mergeSnapshot(raw) {
    const safe = projectRun(raw);
    if (safe.status !== 'running') { result = safe; return safe; }
    for (const value of safe.steps) {
      const previous = result.steps.find(item => item.node_id === value.node_id);
      if (!previous || previous.status === 'running' || value.status !== 'running') upsert(value);
    }
    if (!output && safe.output) result.output = safe.output;
    return {...result, steps: [...result.steps]};
  }
  function apply(frame) {
    if (closed) return {ignored: true, terminal: true};
    const value = frame.data;
    if (!value || typeof value !== 'object' || value.schema_version !== 1 || !positive(value.run_id) || value.version_id !== context.version || value.revision !== context.revision || value.request_id !== context.request_id || run && value.run_id !== run) throw testStreamError('TEST_STREAM_CONTEXT');
    if (!positive(value.seq) || value.seq !== seq + 1 || frame.id !== `${value.run_id}:${value.seq}`) throw testStreamError('TEST_STREAM_SEQUENCE');
    const payload = value.payload;
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw testStreamError();
    run = value.run_id; seq = value.seq;
    let needsResult = false, finalResult = null, observerError = null;
    if (frame.event === 'quick.run_started') {
      if (opened || seq !== 1 || payload.status !== 'running') throw testStreamError();
      opened = true;
    } else if (frame.event === 'quick.run_snapshot') {
      if (opened || seq !== 1) throw testStreamError();
      if (payload.result_available === true) {
        if (!['running', ...TERMINAL].includes(payload.status)) throw testStreamError();
        needsResult = true;
      } else { finalResult = projectRun(payload.result); result = finalResult; }
      closed = true;
    } else if (frame.event === 'quick.typed_error') {
      if (!['run', 'subscription'].includes(payload.scope) || typeof payload.error?.type !== 'string') throw testStreamError();
      if (payload.scope === 'subscription') { observerError = safeError(payload.error); closed = true; }
      else result.error = safeError(payload.error);
    } else {
      if (!opened) throw testStreamError();
      if (frame.event === 'quick.step_started') {
        const value = step(payload.step);
        if (value.status !== 'running' || started.has(value.node_id)) throw testStreamError();
        started.add(value.node_id);
        if (!result.steps.some(item => item.node_id === value.node_id && item.status !== 'running')) upsert(value);
      } else if (frame.event === 'quick.step_finished') {
        const value = step(payload.step);
        if (!started.has(value.node_id) || !['completed', 'incomplete', 'failed'].includes(value.status) || !Number.isFinite(value.elapsed_ms) || value.elapsed_ms < 0) throw testStreamError();
        if (payload.result_available === true) { value.result_available = true; needsResult = true; }
        upsert(value);
      } else if (frame.event === 'quick.output_delta') {
        const node = nodes.get(payload.node_id);
        if (!node || !['model', 'format'].includes(node.kind) || !started.has(node.id) || result.steps.find(item => item.node_id === node.id)?.status !== 'running' || !graph.edges?.some(edge => edge.source === node.id && nodes.get(edge.target)?.kind === 'output') || typeof payload.text !== 'string') throw testStreamError();
        output += payload.text;
        if (output.length > 128 * 1024) throw testStreamError('TEST_STREAM_LIMIT');
        result.output = maskTrace(output);
      } else if (frame.event === 'quick.run_finished') {
        if (payload.result_available === true) {
          if (!TERMINAL.includes(payload.status)) throw testStreamError();
          needsResult = true;
        } else {
          finalResult = projectRun(payload.result);
          if (!TERMINAL.includes(finalResult.status)) throw testStreamError();
          result = finalResult;
        }
        closed = true;
      } else throw testStreamError();
    }
    return {run, seq, terminal: closed, needsResult, finalResult, observerError, result: {...result, steps: [...result.steps]}};
  }
  return {apply, mergeSnapshot, get result() { return result; }};
}
