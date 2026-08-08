// 治理中心的「ANILA 可檢索」標記 toggle —— 按不按得下去、按不下去時說不說得出原因。
//
// 這個 app 沒有 vitest／@testing-library（package.json:9 是 `node --test tests/*.test.mjs`），
// 所以照 ingestionFileAccept.test.mjs 已確立的作法：把 .vue 裡的純函式與 handler
// **原始碼抽出來直接評估**，而不是只用 regex 檢查「檔案裡有沒有提到那個字」。
// 前者殺得死「函式寫對了但畫面沒接」與「畫面接了但函式判斷反了」兩種突變，
// 後者只殺得死第一種的一半。
//
// 三條硬規則對應的測試：
//   1. 密等非無機密 → toggle 停用但**仍然顯示**，且原因看得見（不是只有 tooltip）。
//   2. 切換成功 → reload，顯示值一律來自後端回應（不做樂觀更新）。
//   3. 後端拒絕 → `e.response?.data?.detail` **原樣**上畫面（那段訊息裡寫著自救路徑）。
//
// 密等測試不列舉手寫的等級，而是從後端契約
// `services/csp/app/schemas/contracts/classification.py` 的 ClassificationLevel
// 讀出**整個列舉**再逐級掃；後端加了第五級而前端沒跟上，這裡會紅。

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = resolve(HERE, '../../..')

const VIEW_PATH = resolve(HERE, '../src/views/KnowledgeCollectionsView.vue')
const API_PATH = resolve(HERE, '../src/api/ingestionCollections.js')
const CONTRACT_PATH = resolve(
  REPO_ROOT,
  'services/csp/app/schemas/contracts/classification.py',
)

const VIEW_SOURCE = readFileSync(VIEW_PATH, 'utf8')

/** 去掉 `//`、`/* *\/` 與 `<!-- -->` 註解 —— 註解裡提到的字不該讓 guard 假綠。 */
function stripComments(source) {
  return source
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split('\n')
    .filter((line) => !line.trimStart().startsWith('//'))
    .join('\n')
}

// ── 後端契約：整個 ClassificationLevel 列舉 ─────────────────────────────────

/**
 * 從 classification.py 讀出 ClassificationLevel 的**全部**成員值。
 * 只切 ClassificationLevel 那一個 class（同檔下面還有三個 enum，
 * ClassificationEventReason 有 7 個值，掃到會把測試污染成別的東西）。
 */
function backendClassificationLevels() {
  const source = readFileSync(CONTRACT_PATH, 'utf8')
  const start = source.indexOf('class ClassificationLevel')
  assert.notEqual(start, -1, '後端契約裡找不到 class ClassificationLevel')
  const rest = source.slice(start + 'class ClassificationLevel'.length)
  const end = rest.indexOf('\nclass ')
  const body = end === -1 ? rest : rest.slice(0, end)
  const levels = [...body.matchAll(/^ {4}[A-Z_]+ = "([^"]+)"/gm)].map((m) => m[1])
  assert.ok(
    levels.length >= 4,
    `只從契約解析出 ${levels.length} 級（${levels}）—— 解析壞了，密等掃描會變成假綠`,
  )
  return levels
}

const LEVELS = backendClassificationLevels()
const UNCLASSIFIED = LEVELS[0]
const CLASSIFIED_LEVELS = LEVELS.slice(1)

test('後端列舉解析出來的樣子符合 SYSTEM-MAP §8 的四級契約', () => {
  assert.deepEqual(LEVELS, ['無機密', '營業秘密', '密', '機密'])
  assert.equal(UNCLASSIFIED, '無機密')
})

// ── 抽原始碼並評估 ──────────────────────────────────────────────────────────

/**
 * 從 `needle` 起，做 brace matching 取出完整的函式原始碼。
 * 會跳過字串（' " `）與註解裡的括號。被抽的兩個函式刻意不含正規表示式字面量
 * 與樣板字串，掃描器就不必處理那兩種狀態（見 .vue 裡的註解）。
 */
function extractFunctionSource(source, needle) {
  const start = source.indexOf(needle)
  assert.notEqual(start, -1, `在 KnowledgeCollectionsView.vue 找不到 ${needle.trim()}`)
  let i = source.indexOf('{', start)
  assert.notEqual(i, -1, `${needle.trim()} 後面找不到函式主體`)

  let depth = 0
  let state = 'code'
  for (; i < source.length; i += 1) {
    const ch = source[i]
    const next = source[i + 1]
    if (state === 'line') {
      if (ch === '\n') state = 'code'
      continue
    }
    if (state === 'block') {
      if (ch === '*' && next === '/') { state = 'code'; i += 1 }
      continue
    }
    if (state !== 'code') {
      // 字串內：跳過跳脫字元，遇到同款引號收尾。
      if (ch === '\\') { i += 1; continue }
      if (ch === state) state = 'code'
      continue
    }
    if (ch === '/' && next === '/') { state = 'line'; i += 1; continue }
    if (ch === '/' && next === '*') { state = 'block'; i += 1; continue }
    if (ch === "'" || ch === '"' || ch === '`') { state = ch; continue }
    if (ch === '{') depth += 1
    else if (ch === '}') {
      depth -= 1
      if (depth === 0) {
        const extracted = source.slice(start, i + 1)
        assert.ok(extracted.length > 0)
        return extracted.trimStart()
      }
    }
  }
  assert.fail(`${needle.trim()} 的大括號沒有收斂 —— 抽取器壞了`)
}

/** view 裡的模組層常數（測試注入回去，順便當漂移守衛）。 */
function viewConstant(name) {
  const m = new RegExp(`const ${name} = '([^']*)'`).exec(VIEW_SOURCE)
  assert.ok(m, `KnowledgeCollectionsView.vue 找不到常數 ${name}`)
  return m[1]
}

const VIEW_UNCLASSIFIED = viewConstant('ANILA_MARK_UNCLASSIFIED')

const anilaMarkState = new Function(
  'ANILA_MARK_UNCLASSIFIED',
  `return (${extractFunctionSource(VIEW_SOURCE, '\nfunction anilaMarkState(')})`,
)(VIEW_UNCLASSIFIED)

/** 把 handler 接到假的 API 層／假的 ref 上跑真的一遍。 */
function buildToggle({ updateCollection, isAdmin = true }) {
  const calls = []
  const markErrors = { value: {} }
  const markingId = { value: null }
  const deps = {
    anilaMarkState,
    isAdmin: { value: isAdmin },
    markErrors,
    markingId,
    updateCollection: async (id, patch) => {
      calls.push({ fn: 'updateCollection', id, patch })
      return updateCollection(id, patch)
    },
    loadCollections: async () => { calls.push({ fn: 'loadCollections' }) },
  }
  const names = Object.keys(deps)
  const toggle = new Function(
    ...names,
    `return (${extractFunctionSource(VIEW_SOURCE, '\nasync function toggleAnilaSearchable(')})`,
  )(...names.map((n) => deps[n]))
  return { toggle, calls, markErrors, markingId }
}

function collection(overrides = {}) {
  return {
    id: 42,
    name: '院級法規庫',
    classification_level: UNCLASSIFIED,
    anila_searchable: false,
    origin: 'csp',
    embedding_model: 'nvidia/nv-embed-v2',
    ...overrides,
  }
}

// ── 密等掃描：整個列舉，一級都不能漏 ────────────────────────────────────────

test('未標記時只有無機密可以按，其餘每一級都停用', () => {
  const ok = anilaMarkState(collection({ classification_level: UNCLASSIFIED }), true)
  assert.equal(ok.allowed, true, '無機密的庫竟然按不下去')
  assert.equal(ok.reason, '', '可以按卻還在講原因')

  assert.ok(CLASSIFIED_LEVELS.length >= 3, '掃描的等級數不對')
  for (const level of CLASSIFIED_LEVELS) {
    const state = anilaMarkState(collection({ classification_level: level }), true)
    assert.equal(state.allowed, false, `密等「${level}」竟然可以標記 —— 資料庫層擋著，按了只會拿到 400`)
    assert.ok(state.reason, `密等「${level}」停用了卻沒說原因`)
    assert.ok(
      state.reason.includes(level),
      `密等「${level}」的停用原因沒指名是哪一級：${state.reason}`,
    )
    assert.ok(
      state.reason.includes('降密'),
      `密等「${level}」的停用原因沒給出路（降密申請流程）：${state.reason}`,
    )
  }
})

test('已標記的庫在每一級都還能取消標記 —— 自救出口不可以被自己擋住', () => {
  // 後端 `_guard_anila_searchable` 只在開啟時跑（collections.py:143 的 docstring
  // 寫得很白：「關閉永遠放行」）。前端如果把密等檢查套到兩個方向，
  // 拒絕訊息叫人「先取消標記」就變成一條不存在的路。
  for (const level of LEVELS) {
    const state = anilaMarkState(
      collection({ classification_level: level, anila_searchable: true }),
      true,
    )
    assert.equal(state.allowed, true, `已標記且密等「${level}」的庫竟然不能取消標記`)
  }
})

test('非管理員在每一級、兩個方向都停用，且原因指向管理員', () => {
  // 後端的 admin 閘（collections.py:676）在 flip 算出來之前就擋，兩個方向都擋。
  for (const level of LEVELS) {
    for (const marked of [false, true]) {
      const state = anilaMarkState(
        collection({ classification_level: level, anila_searchable: marked }),
        false,
      )
      assert.equal(state.allowed, false, `非管理員在密等「${level}」marked=${marked} 竟然可以按`)
      assert.ok(state.reason.includes('管理員'), `原因沒指向管理員：${state.reason}`)
    }
  }
})

test('後端沒給密等時當無機密處理（CollectionResponse 的預設值就是無機密）', () => {
  const state = anilaMarkState({ id: 1, anila_searchable: false }, true)
  assert.equal(state.allowed, true)
})

test('marked 旗標一律從資料來，不是自己記的', () => {
  assert.equal(anilaMarkState(collection({ anila_searchable: true }), true).marked, true)
  assert.equal(anilaMarkState(collection({ anila_searchable: false }), true).marked, false)
})

// ── 常數漂移守衛 ────────────────────────────────────────────────────────────

test('view 的密等清單與無機密常數要跟後端契約一致', () => {
  assert.equal(VIEW_UNCLASSIFIED, UNCLASSIFIED)
  const m = /const CLASSIFICATION_LEVELS = \[([^\]]*)\]/.exec(VIEW_SOURCE)
  assert.ok(m, '找不到 CLASSIFICATION_LEVELS')
  const listed = m[1].split(',').map((s) => s.trim().replace(/^'|'$/g, '')).filter(Boolean)
  assert.deepEqual(listed, LEVELS, '前端密等清單跟後端列舉漂開了')
})

// ── handler 行為 ────────────────────────────────────────────────────────────

test('未標記 → 標記：送出 PATCH anila_searchable=true，然後 reload', async () => {
  const c = collection()
  const { toggle, calls } = buildToggle({ updateCollection: async () => ({ data: {} }) })
  await toggle(c)
  assert.deepEqual(calls.map((x) => x.fn), ['updateCollection', 'loadCollections'])
  assert.equal(calls[0].id, 42)
  assert.deepEqual(calls[0].patch, { anila_searchable: true })
})

test('標記 → 未標記：送出 PATCH anila_searchable=false，然後 reload', async () => {
  const c = collection({ anila_searchable: true })
  const { toggle, calls } = buildToggle({ updateCollection: async () => ({ data: {} }) })
  await toggle(c)
  assert.deepEqual(calls[0].patch, { anila_searchable: false })
  assert.deepEqual(calls.map((x) => x.fn), ['updateCollection', 'loadCollections'])
})

test('切換成功不改手上那筆資料 —— 顯示值等下一輪 LIST 回來的那個', async () => {
  const c = collection()
  const { toggle } = buildToggle({ updateCollection: async () => ({ data: { anila_searchable: true } }) })
  await toggle(c)
  assert.equal(
    c.anila_searchable,
    false,
    '樂觀更新：畫面已經說標記了，而真正的值還沒回來（顯示值≠生效值）',
  )
})

test('後端拒絕時，detail 原樣上畫面，且不動顯示值', async () => {
  const detail =
    '此庫用 A，已標記集用 B；跨嵌入空間的分數不能互相比較。'
    + '既有知識庫沒有辦法換嵌入模型（平台沒有 reindex，改 embedding_model 也不會生效），'
    + '請用 B 另建一個知識庫、重新上傳這批文件，再標記那一個。'
  const c = collection()
  const { toggle, calls, markErrors } = buildToggle({
    updateCollection: async () => { throw { response: { data: { detail } } } },
  })
  await toggle(c)
  const shown = markErrors.value[c.id]
  assert.ok(shown, '後端拒絕了而畫面上什麼都沒有')
  assert.ok(
    shown.includes(detail),
    `後端訊息被改寫了，自救路徑掉了：\n收到：${shown}\n應含：${detail}`,
  )
  assert.equal(c.anila_searchable, false)
  assert.deepEqual(calls.map((x) => x.fn), ['updateCollection'], '拒絕了還去 reload')
})

test('先失敗再成功：舊的失敗訊息必須從畫面上消失', async () => {
  // 驗收探針 R2：把 handler 開頭那行 `markErrors.value[c.id] = ''` 拿掉，
  // 原本 24 個測試沒有一個會紅。而 `loadCollections()` **不會**重設 markErrors，
  // 所以那則舊訊息會永久留在一張剛剛成功、卡頭已經掛上「ANILA 可檢索」徽章的卡片旁邊：
  // 徽章說標記好了、下面說標記失敗，管理員會相信下面那句。
  // 這是「以為失敗了，其實成功了」——本專案點名那個形狀的鏡像。
  // 可達路徑：後端守門 (4)（庫內有密等文件）拒絕 → 管理員把那份文件搬走 → 回來再按一次 → 成功。
  const detail = '此庫內含密等「機密」的文件。標記後這些文件不會被檢索，但請先確認它們是否應該留在這個庫裡。'
  const c = collection()
  let attempt = 0
  const { toggle, calls, markErrors } = buildToggle({
    updateCollection: async () => {
      attempt += 1
      if (attempt === 1) throw { response: { data: { detail } } }
      return { data: {} }
    },
  })

  await toggle(c)
  assert.ok(markErrors.value[c.id].includes(detail), '第一次拒絕沒顯示出來，這個測試就白測了')

  await toggle(c)
  assert.ok(
    !markErrors.value[c.id],
    '標記成功了，舊的「標記失敗」還留在卡片上——徽章說成功、下面說失敗，看的人會相信失敗的那句',
  )
  assert.deepEqual(
    calls.map((x) => x.fn),
    ['updateCollection', 'updateCollection', 'loadCollections'],
    '第二次應該成功並 reload',
  )
})

test('後端沒給 detail 時退回 e.message，不要吞掉', async () => {
  const { toggle, markErrors } = buildToggle({
    updateCollection: async () => { throw new Error('Network Error') },
  })
  await toggle(collection())
  assert.ok(markErrors.value[42].includes('Network Error'))
})

test('停用狀態下就算被按到也不送 PATCH', async () => {
  const c = collection({ classification_level: LEVELS[LEVELS.length - 1] })
  const { toggle, calls } = buildToggle({
    updateCollection: async () => { assert.fail('停用的控制項竟然送出了 PATCH') },
  })
  await toggle(c)
  assert.deepEqual(calls, [])
})

test('非管理員按到也不送 PATCH', async () => {
  const { toggle, calls } = buildToggle({
    updateCollection: async () => { assert.fail('非管理員竟然送出了 PATCH') },
    isAdmin: false,
  })
  await toggle(collection())
  assert.deepEqual(calls, [])
})

test('切換結束後 in-flight 旗標要放掉（失敗也要）', async () => {
  const ok = buildToggle({ updateCollection: async () => ({ data: {} }) })
  await ok.toggle(collection())
  assert.equal(ok.markingId.value, null)
  const bad = buildToggle({ updateCollection: async () => { throw new Error('boom') } })
  await bad.toggle(collection())
  assert.equal(bad.markingId.value, null, 'PATCH 失敗後按鈕永遠轉圈圈')
})

// ── 畫面真的接了 ────────────────────────────────────────────────────────────

const CLEAN_VIEW = stripComments(VIEW_SOURCE)
const FOOTER = CLEAN_VIEW.slice(
  CLEAN_VIEW.indexOf('<footer class="cc__foot">'),
  CLEAN_VIEW.indexOf('</footer>'),
)

test('footer 裡有這個 toggle，而且它的 disabled 綁在判斷結果上', () => {
  assert.ok(FOOTER.length > 0, '找不到 cc__foot')
  assert.match(FOOTER, /@click="toggleAnilaSearchable\(c\)"/, 'toggle 沒有接上 handler')
  assert.match(
    FOOTER,
    /:disabled="[^"]*!markStates\[c\.id\]\.allowed/,
    'disabled 沒有綁在 anilaMarkState 的結果上',
  )
})

test('停用時控制項不可以整個藏起來', () => {
  const button = /<button[^>]*toggleAnilaSearchable[^>]*>/.exec(
    FOOTER.replace(/\n/g, ' '),
  )
  assert.ok(button, '找不到 toggle 按鈕')
  assert.doesNotMatch(button[0], /v-if|v-show/, '停用的控制項被藏起來了 —— 硬規則 1')
})

test('停用原因要看得見，不能只掛在 tooltip 上', () => {
  assert.match(
    FOOTER,
    /\{\{\s*markStates\[c\.id\]\.reason\s*\}\}/,
    'reason 沒有以文字節點渲染出來（只有 :title 的話，滑鼠不停在上面的人永遠不知道為什麼）',
  )
})

test('後端拒絕訊息要渲染在這張卡上', () => {
  assert.match(FOOTER, /\{\{\s*markErrors\[c\.id\]\s*\}\}/)
})

test('markStates 是由 anilaMarkState 與 isAdmin 算出來的', () => {
  assert.match(CLEAN_VIEW, /const markStates = computed\(/)
  const block = CLEAN_VIEW.slice(CLEAN_VIEW.indexOf('const markStates = computed('))
  assert.match(block.slice(0, 400), /anilaMarkState\(c, isAdmin\.value\)/)
})

test('顯示值一律來自後端資料：程式碼裡不得對 anila_searchable 賦值', () => {
  assert.doesNotMatch(
    CLEAN_VIEW,
    /\.anila_searchable\s*=[^=]/,
    '有人對 anila_searchable 賦值 —— 那就是樂觀更新',
  )
})

test('handler 一定要在 PATCH 之後 reload', () => {
  const handler = extractFunctionSource(VIEW_SOURCE, '\nasync function toggleAnilaSearchable(')
  const patchAt = handler.indexOf('updateCollection(')
  const reloadAt = handler.indexOf('loadCollections(')
  assert.ok(patchAt !== -1 && reloadAt !== -1, 'handler 沒有 PATCH 或沒有 reload')
  assert.ok(reloadAt > patchAt, 'reload 排在 PATCH 前面')
})

test('卡片上看得出目前有沒有標記，而且那個值來自後端欄位', () => {
  assert.match(CLEAN_VIEW, /c\.anila_searchable\s*\?/, '畫面上沒有用後端的旗標決定顯示')
})

// ── API 層 ─────────────────────────────────────────────────────────────────

test('updateCollection 的 JSDoc 要寫出 anila_searchable', () => {
  const src = readFileSync(API_PATH, 'utf8')
  const block = src.slice(0, src.indexOf('export const updateCollection'))
  const jsdoc = block.slice(block.lastIndexOf('/**'))
  assert.match(
    jsdoc,
    /anila_searchable\?: boolean/,
    'JSDoc 沒列 anila_searchable，讀起來像「不支援」',
  )
})
