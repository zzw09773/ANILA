import test from 'node:test';
import assert from 'node:assert/strict';
import './helpers/dom.mjs';
import { createApp, nextTick, h } from 'vue';
import { build } from 'vite';
import vue from '@vitejs/plugin-vue';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { resolve, dirname } from 'node:path';
import * as fixture from './helpers/actorIsolationMock.mjs';
import { invalidateIdentity, announceIdentity, currentIdentityEpoch } from '../src/api/identityEpoch.js';
import { _dialogState, _settleConfirm, clearActorDialogs, useDialog } from '../src/composables/useDialog.js';
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const components = {};
let app;
const settle = async () => {
  for (let i = 0; i < 20; i++) {
    await Promise.resolve();
    await nextTick();
  }
};
const field = text => [...document.querySelectorAll('label')].find(n => n.firstChild?.textContent.includes(text))?.querySelector('input,textarea,select');
const button = text => [...document.querySelectorAll('button')].find(n => n.textContent.trim() === text || n.firstChild?.textContent.trim() === text);
async function change(node, value) {
  assert.ok(node);
  node.value = value;
  node.dispatchEvent(new Event(node.tagName === 'SELECT' ? 'change' : 'input', {
    bubbles: true
  }));
  await settle();
}
async function click(text) {
  assert.ok(button(text), text);
  button(text).click();
  await settle();
}
async function mount(name = 'builder', {
  host = true
} = {}) {
  const el = document.createElement('div');
  document.body.append(el);
  app = createApp({
    render: () => h('div', [h(components[name]), host ? h(components.host) : null])
  });
  app.mount(el);
  await settle();
}
function neutral() {
  assert.ok(document.querySelector('[data-actor-cleared]'));
  assert.equal(document.querySelectorAll('.qb input,.qb textarea,.qb select,.qb option,.qb details,.qb .qb-node').length, 0);
  assert.doesNotMatch(document.body.outerHTML, /CANARY|規章問答|平台核准模型|採購規章|請假餘額|單位規章|差勤服務|mcp.internal|查假/);
  assert.doesNotMatch([...document.querySelectorAll('input,textarea,select')].map(n => n.value).join(' '), /CANARY/);
}
async function open() {
  await change(field('助手'), '11');
}
async function fillDraft() {
  await open();
  await change(field('助手名稱'), 'A_TITLE_CANARY');
  await change(field('用途說明'), 'A_DESCRIPTION_CANARY');
  await click('測試對話');
  await change(field('測試問題'), 'A_QUESTION_CANARY');
}
async function compile(entry) {
  const require = createRequire(import.meta.url);
  const mock = new URL('./helpers/actorIsolationMock.mjs', import.meta.url).href;
  const result = await build({
    configFile: false,
    envDir: false,
    root: ROOT,
    logLevel: 'silent',
    plugins: [{
      name: 'isolation-mocks',
      enforce: 'pre',
      resolveId(id, importer) {
        if (id === './client' && importer?.includes('/api/')) return {
          id: 'actor-mock',
          external: true
        };
        if (id.includes('/composables/useDialog')) return {
          id: 'actor-dialog',
          external: true
        };
        if (id.includes('/api/identityEpoch')) return {
          id: 'actor-identity',
          external: true
        };
        if (id === 'vue-router' && entry.includes('AppHeader')) return {
          id: 'actor-mock',
          external: true
        };
        if (id.endsWith('/api/caAuth')) return {
          id: 'actor-mock',
          external: true
        };
        if (id.endsWith('/stores/auth.js')) return {
          id: 'actor-mock',
          external: true
        };
        if (id.endsWith('/api/quickBuilder') || id.endsWith('/api/client') || id.endsWith('/stores/auth') || id.endsWith('/api/auth') || id.endsWith('/composables/useTheme') || id.endsWith('/composables/useShellNav.js')) return {
          id: 'actor-mock',
          external: true
        };
      }
    }, vue()],
    build: {
      write: false,
      minify: false,
      lib: {
        entry: resolve(ROOT, entry),
        formats: ['es']
      },
      rollupOptions: {
        external: ['vue', ...(entry.includes('AppHeader') ? [] : ['vue-router'])],
        output: {
          inlineDynamicImports: true,
          paths: {
            vue: pathToFileURL(require.resolve('vue')).href,
            'vue-router': pathToFileURL(require.resolve('vue-router')).href,
            'actor-mock': mock,
            'actor-dialog': new URL('../src/composables/useDialog.js', import.meta.url).href,
            'actor-identity': new URL('../src/api/identityEpoch.js', import.meta.url).href
          }
        }
      }
    }
  });
  const chunk = (Array.isArray(result) ? result[0] : result).output.find(c => c.type === 'chunk' && c.isEntry);
  return (await import(`data:text/javascript;base64,${Buffer.from(chunk.code).toString('base64')}`)).default;
}
test.before(async () => {
  for (const [name, entry] of Object.entries({
    builder: 'src/views/QuickBuilderView.vue',
    mcp: 'src/views/QuickBuilderMcpView.vue',
    header: 'src/components/layout/AppHeader.vue',
    host: 'src/components/cli/TermDialogHost.vue'
  })) components[name] = await compile(entry);
});
test.beforeEach(() => {
  fixture.resetIsolation();
  clearActorDialogs();
  window.sessionStorage.clear();
  window.confirm = () => {
    throw new Error('原生確認不應執行');
  };
  window.alert = () => {
    throw new Error('原生提示不應執行');
  };
});
test.afterEach(() => {
  app?.unmount();
  app = null;
  clearActorDialogs();
  document.body.replaceChildren();
});
test('跨分頁invalid通知在/me前清空全部草稿、問題、設定、選單，A→B→A不復原', async () => {
  await mount();
  await fillDraft();
  invalidateIdentity();
  await settle();
  neutral();
  fixture.changeActor(202);
  announceIdentity(202);
  await settle();
  neutral();
  fixture.changeActor(101);
  announceIdentity(101);
  await settle();
  neutral();
  assert.equal(fixture.calls.filter(c => c[0] === 'save' || c[0] === 'create').length, 0);
  assert.doesNotMatch(JSON.stringify(window.sessionStorage), /CANARY/);
  app.unmount();
  app = null;
  document.body.replaceChildren();
  await mount();
  await open();
  assert.equal(field('助手名稱').value, '規章問答');
  assert.equal(field('用途說明').value, '單位規章問答');
  assert.doesNotMatch(document.body.textContent, /CANARY/);
});
for (const held of ['getAssistant', 'createAssistant', 'compareVersions']) test(`待處理 ${held} 晚回不能復活舊actor資料`, async () => {
  await mount();
  if (held === 'createAssistant') {
    await change(field('助手名稱'), 'A_TITLE_CANARY');
    await change(field('平台模型'), '1');
    fixture.hold(held);
    await click('儲存新版本');
  } else if (held === 'getAssistant') {
    fixture.hold(held);
    await open();
  } else {
    await open();
    await click('版本與審核');
    await change(field('較早版本'), '21');
    await change(field('較新版本'), '22');
    fixture.hold(held);
    await click('比較版本');
  }
  invalidateIdentity();
  await settle();
  neutral();
  fixture.release(held);
  await settle();
  neutral();
  assert.equal(fixture.observed.filter(c => c[0] === 'getCatalog').length, held === 'getAssistant' ? 2 : held === 'compareVersions' ? 2 : 1);
});
test('初次catalog尚未回覆時收到null身分也不能綁入舊actor', async () => {
  fixture.hold('getCatalog');
  await mount();
  invalidateIdentity();
  await settle();
  neutral();
  fixture.release('getCatalog');
  await settle();
  neutral();
});
for (const status of [401, 403, 404]) test(`助手資料GET ${status} 清除整份DOM而非只清答案`, async () => {
  await mount();
  await fillDraft();
  fixture.deny('getAssistant', status);
  await click('重新整理');
  assert.equal(_dialogState.confirm.open, true);
  _settleConfirm(true);
  await settle();
  neutral();
  assert.match(document.body.textContent, /ACCESS_CHANGED/);
  assert.equal(fixture.calls.filter(c => c[0] === 'save').length, 0);
});
test('pagehide立即失效並清空，persisted pageshow不復活也不廣播他頁身分', async () => {
  await mount();
  await fillDraft();
  fixture.hold('getAssistant');
  await click('重新整理');
  _settleConfirm(true);
  await settle();
  const before = fixture.observed.length;
  window.dispatchEvent(new Event('pagehide'));
  await settle();
  neutral();
  window.dispatchEvent(new PageTransitionEvent('pageshow', {
    persisted: true
  }));
  fixture.release('getAssistant');
  await settle();
  neutral();
  assert.equal(fixture.observed.length, before);
});
test('非阻塞捨棄確認在換帳時撤回，晚確認不讀取或改動B', async () => {
  await mount();
  await fillDraft();
  await click('建立助手');
  assert.equal(_dialogState.confirm.open, true);
  invalidateIdentity();
  await settle();
  neutral();
  assert.equal(_dialogState.confirm.open, false);
  _settleConfirm(true);
  await settle();
  neutral();
  assert.equal(fixture.calls.filter(c => c[0] === 'create' || c[0] === 'save').length, 0);
});
test('管理工具的服務、schema、草稿與非阻塞停用確認均清除，晚POST不復活', async () => {
  await mount('mcp');
  await change(field('服務名稱'), 'A_MCP_CANARY');
  await change(field('院內服務網址'), 'https://A_URL_CANARY.test');
  await click('停用此服務');
  assert.equal(_dialogState.confirm.open, true);
  assert.match(_dialogState.confirm.message, /差勤服務/);
  invalidateIdentity();
  await settle();
  neutral();
  _settleConfirm(true);
  await settle();
  assert.equal(fixture.observed.some(c => c[0] === 'mcpPost'), false);
});
test('管理工具登錄晚回不重新載入或顯示成功', async () => {
  await mount('mcp');
  fixture.hold('mcpPost');
  document.querySelector('form').dispatchEvent(new Event('submit', {
    bubbles: true,
    cancelable: true
  }));
  await settle();
  invalidateIdentity();
  await settle();
  neutral();
  fixture.release('mcpPost');
  await settle();
  neutral();
  assert.equal(fixture.observed.filter(c => c[0] === 'mcpGet').length, 1);
});
test('global typed confirm與toast在身分失效時清空全部slot及state，舊resolver只能false', async () => {
  await mount('host', {
    host: false
  });
  const pending = useDialog().confirm({
    title: 'A_DIALOG_CANARY',
    message: 'A_MESSAGE_CANARY',
    requireText: 'A_REQUIRE_CANARY'
  });
  _dialogState.confirm.input = 'A_INPUT_CANARY';
  useDialog().toast('A_TOAST_CANARY', {
    duration: 100000
  });
  await settle();
  assert.match(document.body.textContent, /A_MESSAGE_CANARY/);
  invalidateIdentity();
  await settle();
  assert.equal(await pending, false);
  assert.equal(document.querySelector('[role=dialog]'), null);
  assert.doesNotMatch(JSON.stringify(_dialogState), /CANARY/);
  assert.doesNotMatch(document.body.textContent, /CANARY/);
  assert.equal(_dialogState.toasts.length, 0);
  useDialog().toast('B的通知', {
    duration: 100000
  });
  await settle();
  assert.equal(_dialogState.toasts[0].message, 'B的通知');
});
async function password() {
  await mount('header', {
    host: false
  });
  await click('變更密碼');
  const inputs = [...document.querySelectorAll('input[type=password]')];
  assert.equal(inputs.length, 3);
  for (const [i, value] of ['A_CURRENT_CANARY', 'A_New_CANARY!', 'A_New_CANARY!'].entries()) await change(inputs[i], value);
}
for (const outcome of ['success', 'failure']) test(`Header密碼${outcome}晚回不顯舊內容、不登出B，A→B→A也不恢復`, async () => {
  await password();
  fixture.hold('changePassword');
  if (outcome === 'failure') fixture.deny('changePassword', 500);
  await click('更新');
  invalidateIdentity();
  fixture.auth.user = {
    id: 202,
    username: 'B',
    role: 'admin'
  };
  await settle();
  assert.equal(document.querySelectorAll('input[type=password]').length, 0);
  fixture.release('changePassword');
  await settle();
  assert.doesNotMatch(document.body.textContent, /CANARY|密碼已更新|A_ERROR/);
  fixture.auth.user = {
    id: 101,
    username: 'A',
    role: 'admin'
  };
  await click('變更密碼');
  assert.deepEqual([...document.querySelectorAll('input[type=password]')].map(n => n.value), ['', '', '']);
  assert.equal(fixture.observed.some(c => c[0] === 'logout'), false);
});
test('Header已排程的成功登出在換帳及pagehide後不能作用於B', async () => {
  await password();
  const original = globalThis.setTimeout,
    originalClear = globalThis.clearTimeout;
  let timer,
    cleared = false;
  globalThis.setTimeout = (fn, ms, ...args) => ms === 1500 ? (timer = fn, 998) : original(fn, ms, ...args);
  globalThis.clearTimeout = id => {
    if (id === 998) cleared = true;else originalClear(id);
  };
  try {
    await click('更新');
    assert.match(document.body.textContent, /密碼已更新/);
    window.dispatchEvent(new Event('pagehide'));
    fixture.auth.user = {
      id: 202,
      username: 'B',
      role: 'admin'
    };
    await settle();
    assert.equal(cleared, true);
    timer();
    await settle();
    assert.equal(fixture.observed.some(c => c[0] === 'logout' || c[0] === 'navigate'), false);
    assert.equal(document.querySelectorAll('input[type=password]').length, 0);
    assert.doesNotMatch(document.body.textContent, /CANARY|密碼已更新/);
  } finally {
    globalThis.setTimeout = original;
    globalThis.clearTimeout = originalClear;
  }
});
test('舊作用域的晚confirm/toast不能重開A；新B作用域仍能正常確認', async () => {
  await mount('host', {
    host: false
  });
  const old = useDialog();
  invalidateIdentity();
  await settle();
  old.toast('A_LATE_TOAST_CANARY');
  assert.equal(await old.confirm('A_LATE_CONFIRM_CANARY'), false);
  await settle();
  assert.doesNotMatch(document.body.textContent, /CANARY/);
  assert.equal(_dialogState.confirm.open, false);
  const fresh = useDialog();
  const accepted = fresh.confirm('B的確認');
  await settle();
  assert.match(document.body.textContent, /B的確認/);
  _settleConfirm(true);
  assert.equal(await accepted, true);
});
for (const status of [401, 403, 404]) test(`Header更新 ${status} 清掉密碼modal，晚回不能留下秘密`, async () => {
  await password();
  fixture.deny('changePassword', status);
  await click('更新');
  assert.equal(document.querySelectorAll('input[type=password]').length, 0);
  assert.doesNotMatch(document.body.textContent, /A_CURRENT_CANARY|A_New_CANARY|A_ERROR/);
  assert.equal(fixture.observed.some(c => c[0] === 'logout'), false);
});
test('換tab visibilitychange不清稿，實際pagehide才清除', async () => {
  await mount();
  await fillDraft();
  document.dispatchEvent(new Event('visibilitychange'));
  await settle();
  assert.equal(field('助手名稱').value, 'A_TITLE_CANARY');
  window.dispatchEvent(new Event('pagehide'));
  await settle();
  neutral();
});
test('真storage跨分頁通知在/me未回時即卸載，不靠下一次寫入檢測', async () => {
  await mount();
  await fillDraft();
  window.dispatchEvent(new StorageEvent('storage', {
    key: 'anila:identity-epoch',
    newValue: JSON.stringify({
      version: 1,
      epoch: 'f0a99b5a-cf12-46c0-9851-5bfce5399446'
    })
  }));
  await settle();
  neutral();
  assert.equal(fixture.calls.some(c => c[0] === 'create' || c[0] === 'save'), false);
});
for (const status of [401, 403, 404]) test(`寫入 ${status} 清除全部表單及服務自由錯誤內容`, async () => {
  await mount();
  await change(field('助手名稱'), 'A_POST_CANARY');
  await change(field('用途說明'), 'A_DESCRIPTION_CANARY');
  await change(field('平台模型'), '1');
  fixture.deny('createAssistant', status);
  await click('儲存新版本');
  neutral();
  assert.match(document.body.textContent, /ACCESS_CHANGED/);
  assert.equal(fixture.observed.filter(c => c[0] === 'createAssistant').length, 1);
});
test('pagehide僅撤銷本地展示世代，不改全域身分epoch或廣播登出', async () => {
  await mount();
  await fillDraft();
  const epoch = currentIdentityEpoch();
  window.dispatchEvent(new Event('pagehide'));
  await settle();
  neutral();
  assert.equal(currentIdentityEpoch(), epoch);
});
test('提示設定/節點名稱/模型選項與展開明細一起清除', async () => {
  fixture.setGraph({
    nodes: [{
      id: 'in',
      kind: 'input',
      label: 'A_INPUT_CANARY',
      position: {
        x: 0,
        y: 0
      },
      config: {
        placeholder: 'A_PLACEHOLDER_CANARY'
      }
    }, {
      id: 'p',
      kind: 'prompt',
      label: 'A_PROMPT_NODE_CANARY',
      position: {
        x: 0,
        y: 220
      },
      config: {
        instruction: 'A_INSTRUCTION_CANARY',
        role: 'assistant'
      }
    }, {
      id: 'out',
      kind: 'output',
      label: 'A_OUTPUT_CANARY',
      position: {
        x: 0,
        y: 440
      },
      config: {}
    }],
    edges: [{
      source: 'in',
      target: 'p'
    }, {
      source: 'p',
      target: 'out'
    }]
  });
  await mount();
  await open();
  document.querySelector('[aria-label="選取A_PROMPT_NODE_CANARY；方向鍵移動"]').click();
  await settle();
  assert.equal(field('工作指示').value, 'A_INSTRUCTION_CANARY');
  for (const details of document.querySelectorAll('details')) details.open = true;
  invalidateIdentity();
  await settle();
  neutral();
});
test('初次A catalog待回時先驗證B正值身分，晚A catalog不得綁定', async () => {
  fixture.hold('getCatalog');
  await mount();
  fixture.changeActor(202);
  announceIdentity(202);
  await settle();
  fixture.release('getCatalog');
  await settle();
  neutral();
});
test('初次A catalog待回時驗證同A正值身分仍可正常載入', async () => {
  fixture.hold('getCatalog');
  await mount();
  announceIdentity(101);
  await settle();
  fixture.release('getCatalog');
  await settle();
  assert.equal(document.querySelector('[data-actor-cleared]'), null);
  await open();
  assert.equal(field('助手名稱').value, '規章問答');
});
for (const kind of ['input', 'compositionend', 'click']) test(`舊確認實例晚${kind}不影響新B確認`, async () => {
  await mount('host', {
    host: false
  });
  const a = useDialog().confirm({
    message: 'A_OLD_CANARY',
    requireText: 'A_GATE_CANARY'
  });
  await settle();
  const oldInput = document.querySelector('.term-confirm__input');
  oldInput.dispatchEvent(new Event('compositionstart', {
    bubbles: true
  }));
  const oldButton = button('確定');
  invalidateIdentity();
  await settle();
  assert.equal(await a, false);
  let outcome = 'pending';
  const b = useDialog().confirm({
    message: 'B的確認',
    requireText: 'B的文字'
  }).then(value => outcome = value);
  await settle();
  oldInput.value = 'A_LATE_INPUT_CANARY';
  if (kind === 'click') oldButton.dispatchEvent(new MouseEvent('click', {
    bubbles: true
  }));else {
    if (kind === 'input') oldInput.composing = false;
    oldInput.dispatchEvent(new Event(kind, {
      bubbles: true
    }));
  }
  await settle();
  assert.equal(_dialogState.confirm.input, '');
  assert.equal(outcome, 'pending');
  assert.doesNotMatch(document.body.outerHTML, /CANARY/);
  _settleConfirm(false);
  await b;
});
test('全域對話框正值A→B也清內容與舊scope，同A通知控制組保持可確認', async () => {
  await mount('host', {
    host: false
  });
  announceIdentity(101);
  const old = useDialog();
  const a = old.confirm({
    message: 'A_DIALOG_CANARY',
    requireText: 'A_GATE_CANARY'
  });
  _dialogState.confirm.input = 'A_GATE_CANARY';
  old.toast('A_TOAST_CANARY', {
    duration: 100000
  });
  announceIdentity(101);
  await settle();
  assert.equal(_dialogState.confirm.open, true);
  assert.equal(_dialogState.confirm.input, 'A_GATE_CANARY');
  announceIdentity(202);
  await settle();
  assert.equal(await a, false);
  assert.doesNotMatch(document.body.outerHTML, /CANARY/);
  assert.equal(await old.confirm('A_LATE_CANARY'), false);
  old.toast('A_LATE_TOAST_CANARY');
  await settle();
  assert.doesNotMatch(JSON.stringify(_dialogState), /CANARY/);
  const b = useDialog().confirm({
    message: 'B的中文確認',
    requireText: '確認'
  });
  await settle();
  const input = document.querySelector('.term-confirm__input');
  input.dispatchEvent(new Event('compositionstart', {
    bubbles: true
  }));
  input.value = '確認';
  input.dispatchEvent(new Event('input', {
    bubbles: true
  }));
  assert.equal(_dialogState.confirm.input, '');
  input.dispatchEvent(new Event('compositionend', {
    bubbles: true
  }));
  await settle();
  assert.equal(_dialogState.confirm.input, '確認');
  input.dispatchEvent(new KeyboardEvent('keydown', {
    key: 'Enter',
    isComposing: true,
    bubbles: true
  }));
  await settle();
  assert.equal(_dialogState.confirm.open, true);
  input.dispatchEvent(new KeyboardEvent('keydown', {
    key: 'Enter',
    bubbles: true
  }));
  await settle();
  assert.equal(await b, true);
});
for (const mode of ['input', 'compositionend']) test(`舊密碼實例晚${mode}不能回填B的三個新欄位`, async () => {
  await password();
  const old = [...document.querySelectorAll('input[type=password]')];
  if (mode === 'compositionend') for (const input of old) input.dispatchEvent(new Event('compositionstart', {
    bubbles: true
  }));
  invalidateIdentity();
  fixture.auth.user = {
    id: 202,
    username: 'B',
    role: 'admin'
  };
  announceIdentity(202);
  await settle();
  assert.equal(document.querySelectorAll('input[type=password]').length, 0);
  await click('變更密碼');
  const expected = ['B_Current_Canary!', 'B_New_Canary!', 'B_New_Canary!'];
  for (const [i, input] of [...document.querySelectorAll('input[type=password]')].entries()) await change(input, expected[i]);
  for (const [i, input] of old.entries()) {
    input.value = `A_LATE_PASSWORD_CANARY_${i}`;
    input.dispatchEvent(new Event(mode, {
      bubbles: true
    }));
  }
  await settle();
  assert.deepEqual([...document.querySelectorAll('input[type=password]')].map(input => input.value), expected);
  assert.doesNotMatch(document.body.outerHTML, /A_LATE_PASSWORD_CANARY/);
  assert.equal(button('更新').disabled, false);
  assert.equal(fixture.observed.some(c => c[0] === 'changePassword'), false);
});
test('舊密碼視窗的更新、取消與關閉事件不能操作B的新視窗，B中文IME仍可提交', async () => {
  await password();
  const oldUpdate = button('更新'),
    oldCancel = button('取消'),
    oldClose = document.querySelector('[aria-label="關閉"]');
  invalidateIdentity();
  fixture.auth.user = {
    id: 202,
    username: 'B',
    role: 'admin'
  };
  announceIdentity(202);
  await settle();
  await click('變更密碼');
  const inputs = [...document.querySelectorAll('input[type=password]')];
  await change(inputs[0], 'B_Current_Canary!');
  inputs[1].dispatchEvent(new Event('compositionstart', {
    bubbles: true
  }));
  inputs[1].value = 'B_中文新密碼_Canary!';
  inputs[1].dispatchEvent(new Event('input', {
    bubbles: true
  }));
  await settle();
  assert.equal(button('更新').disabled, true);
  inputs[1].dispatchEvent(new Event('compositionend', {
    bubbles: true
  }));
  await change(inputs[2], 'B_中文新密碼_Canary!');
  assert.equal(button('更新').disabled, false);
  for (const control of [oldUpdate, oldCancel, oldClose]) {
    control.dispatchEvent(new MouseEvent('click', {
      bubbles: true
    }));
    await settle();
  }
  assert.equal(document.querySelectorAll('input[type=password]').length, 3);
  assert.equal(fixture.observed.some(c => c[0] === 'changePassword'), false);
  fixture.hold('changePassword');
  await click('更新');
  assert.equal(fixture.observed.filter(c => c[0] === 'changePassword').length, 1);
  window.dispatchEvent(new Event('pagehide'));
  fixture.release('changePassword');
  await settle();
  assert.equal(document.querySelectorAll('input[type=password]').length, 0);
  assert.equal(fixture.observed.some(c => c[0] === 'logout'), false);
});
