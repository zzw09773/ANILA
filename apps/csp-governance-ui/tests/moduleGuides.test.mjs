import test from 'node:test';
import assert from 'node:assert/strict';
import { moduleGuide, MODULE_GROUPS } from '../src/quickBuilder/moduleGuides.js';
import { modules } from '../src/quickBuilder/graph.js';

test('11種步驟都有用途、輸入、輸出、行政例子及有效分類', () => {
  assert.equal(modules.length, 11);
  assert.equal(new Set(MODULE_GROUPS.map(group => group.id)).size, MODULE_GROUPS.length);
  for (const {kind} of modules) {
    const guide = moduleGuide(kind);
    for (const key of ['title', 'description', 'input', 'output', 'example', 'technicalName']) {
      assert.equal(typeof guide[key], 'string');
      assert.ok(guide[key].length > 3);
    }
    assert.ok(MODULE_GROUPS.some(group => group.id === guide.group));
    assert.doesNotMatch(guide.title, /無法辨識/);
  }
});
test('未知步驟使用固定安全說明，不回顯任意kind或原型屬性', () => {
  const fallback = moduleGuide('UNTRUSTED_KIND_CANARY');
  for (const kind of [null, undefined, 17, '__proto__', 'constructor', {toString: null}, {toString() {throw new Error('UNTRUSTED_KIND_CANARY');}}, Symbol('UNTRUSTED_KIND_CANARY')]) {
    assert.deepEqual(moduleGuide(kind), fallback);
  }
  assert.doesNotMatch(JSON.stringify(fallback), /UNTRUSTED_KIND_CANARY|__proto__|constructor/);
  assert.match(fallback.title, /無法辨識/);
});
test('指南不可被呼叫端修改，也不改變原始模組與預設值', () => {
  const before = JSON.stringify(modules);
  assert.throws(() => {moduleGuide('rag').description = '變更';}, TypeError);
  assert.throws(() => {MODULE_GROUPS[0].name = '變更';}, TypeError);
  assert.equal(JSON.stringify(modules), before);
});
