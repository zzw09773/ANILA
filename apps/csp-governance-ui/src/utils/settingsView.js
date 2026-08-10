// 設定總覽頁的全部判斷 —— 分區、分態、顯示字串。
//
// 為什麼判斷全在這裡而不在 .vue：這個 app 沒有元件掛載測試（package.json 的
// test 是 `node --test tests/*.test.mjs`），寫在 .vue 裡的判斷等於沒有測試。
// 所以 .vue 只留樣板與呼叫，凡是「畫面要說什麼」的決定都出自本檔，
// 由 tests/settingsOverview.test.mjs 直接 import 驗。
//
// 這一頁只有一條總則：**畫面上的每一句話都要指得出後端 payload 的哪一欄**。
// 後端（task-5-report §1／§2）已經把三個長得像「現在的值」的東西分開了：
//   `effective` = 現在真正生效的、`stored` = DB 那一列的原始字串、
//   `pending`   = 存了但這次開機還沒套上去的（＝重啟後才生效）。
// 前端唯一的工作是**不要把它們畫成同一件事**。

/**
 * 這一頁叫得動的、真的會讓 csp 重讀一次設定的那一道命令。
 *
 * ⚠ **不可以是裸的 `docker compose up -d csp`。** 改一顆設定只寫了一列 DB，
 * compose 檔與容器的設定雜湊**一個字都沒變**，於是 `up -d` 會判定「已是最新」
 * 而**根本不重建容器** —— 開機覆蓋不會重跑、存的值不會生效，而我們卻叫管理員
 * 去執行一道什麼也不做的命令。那正是這一頁存在要消滅的東西（叫得動、
 * 沒報錯、什麼也沒發生）。做法與理由見 `docs/runbooks/settings-page.md`。
 */
export const RECREATE_COMMAND = 'docker compose up -d --force-recreate csp'

/** 存完之後要跟管理員說的那一句（B_EDIT／任何 restart_required 的列）。
 *  後半句是**證明**那一步：這一頁是唯一能當場驗出「到底有沒有生效」的地方。 */
export const RESTART_HINT =
  `已儲存，重啟後生效：${RECREATE_COMMAND}；重開後回頭看這一列，「現在生效」要變成你存的值`

/** C 類存完那一句 —— 下一個請求就會讀到，不必重啟。 */
export const SAVED_NOW_HINT = '已儲存，下一個請求就生效'

/** 後端回了這一頁還不認得的 class 時，那些列的去處。 */
export const UNKNOWN_SECTION_ID = 'unknown-class'

/** 三態。`id`／`label`／`className` 三者都必須互不相同 —— 兩態共用一個樣式，
 *  就是「待生效畫成已生效」那個殺形的原型。 */
const STATES = {
  effective: {
    id: 'effective',
    label: '現在生效',
    className: 'setting-state--effective',
  },
  pending: {
    id: 'pending',
    label: '待生效（重啟後）',
    className: 'setting-state--pending',
  },
  unusable: {
    id: 'unusable',
    label: '存了但讀不回來，永遠不會生效',
    className: 'setting-state--unusable',
  },
}

/** 有值可講的 class。A 類的值在後端就遮掉了，畫面上沒有東西可以分態。 */
const VALUE_BEARING = new Set(['C', 'B_EDIT', 'B_LOCKED', 'SEC'])

/**
 * 四個區，順序就是版面順序。
 *
 * `editable` 是**版面**的性質（這一區能不能放編輯器）；某一列到底給不給編輯器，
 * 一律再問 `canEdit(item)`，也就是後端那個 `editable` 欄位 —— 後端哪天把一顆
 * C 降級而 class 還沒改，畫面要立刻停手。
 */
export const SECTION_DEFS = [
  {
    id: 'apply-now',
    classes: ['C'],
    title: '改完立刻生效',
    hint: '每一次請求都重新讀 DB，按下儲存之後下一個請求就是新值。',
    editable: true,
    showsValues: true,
  },
  {
    id: 'apply-on-restart',
    classes: ['B_EDIT'],
    title: '改得動，但要重啟才生效',
    hint: `存進 DB，下一次開機才會套上去。存完請執行：${RECREATE_COMMAND}`,
    editable: true,
    showsValues: true,
  },
  {
    id: 'locked',
    classes: ['B_LOCKED', 'SEC'],
    title: '這裡改不動',
    hint: '每一列都寫著為什麼改不動，以及真的要改的話該去哪裡改。',
    editable: false,
    showsValues: true,
  },
  {
    id: 'secrets',
    classes: ['A'],
    title: '祕密：只說設了沒有',
    hint: '值不會離開後端 —— 這裡連 default 都看不到，只看得出有沒有人設過。',
    editable: false,
    showsValues: false,
  },
]

const SECTION_BY_CLASS = new Map(
  SECTION_DEFS.flatMap((section) => section.classes.map((cls) => [cls, section.id])),
)

const UNKNOWN_SECTION = {
  id: UNKNOWN_SECTION_ID,
  classes: [],
  title: '這一頁還不認得的類別',
  hint: '後端回了新的 class。在有人決定它能不能顯示、能不能改之前，這裡只列名稱。',
  // 不知道能不能改就不給改，不知道能不能顯示就不顯示 —— 兩邊都取保守的那一側。
  editable: false,
  showsValues: false,
}

/** 這一列該進哪一區。認不得的 class 進溢位區，**絕不丟掉**。 */
export function sectionIdFor(item) {
  return SECTION_BY_CLASS.get(item?.class) ?? UNKNOWN_SECTION_ID
}

/**
 * 把 payload 的 `items` 分進四區（必要時再加一個溢位區）。
 *
 * 區內順序＝後端回的順序（登錄表宣告序，前綴已分群），**不重新排序**：
 * 後端把同一個消費模組的顆數排在一起是有意義的，前端按字母排會把它打散。
 */
export function groupIntoSections(items) {
  const rows = Array.isArray(items) ? items : []
  const buckets = new Map(SECTION_DEFS.map((section) => [section.id, []]))
  const overflow = []

  for (const item of rows) {
    const id = sectionIdFor(item)
    if (buckets.has(id)) buckets.get(id).push(item)
    else overflow.push(item)
  }

  const sections = SECTION_DEFS.map((section) => ({ ...section, items: buckets.get(section.id) }))
  if (overflow.length) sections.push({ ...UNKNOWN_SECTION, items: overflow })
  return sections
}

/** 這一列給不給編輯器 —— 問後端那個欄位，不看 class 猜。 */
export function canEdit(item) {
  return item?.editable === true
}

/**
 * 三態之一，或 `null`（沒有值可講的列：A 類與不認得的類別）。
 *
 * 判序刻意把「讀不回來」放在最前面：那一列存了東西、卻**永遠不會生效**，
 * 是三態裡唯一需要管理員回頭處理的一態，不可以被後面兩態蓋掉。
 */
export function rowState(item) {
  if (!item || !VALUE_BEARING.has(item.class)) return null
  if (item.stored != null && item.stored_usable === false) return STATES.unusable
  if (item.pending != null) return STATES.pending
  return STATES.effective
}

/** 值怎麼寫成畫面上的字。 */
export function formatSettingValue(value) {
  if (value === null || value === undefined) return '—'
  // 空字串不是「沒設」——`ANILA_HOST=""` 與整行不存在，後端讀出來是兩件事。
  if (value === '') return '（空字串）'
  if (typeof value === 'string') return value
  // 布林維持 true／false：後端有五種互不相容的真值判準（== "1"、!= "0"、
  // lower == "true"…），翻成「是／否」等於再發明第六種寫法。
  if (typeof value === 'boolean' || typeof value === 'number') return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

const SOURCE_LABELS = {
  db: 'DB（這一頁存的，已生效）',
  'db-boot': 'DB（開機時套上去的）',
  env: 'env／compose',
  default: '程式預設',
}

/** 來源那一欄。認不得的來源**原樣顯示**，不臆測（沿用健康總覽的作法）。 */
export function sourceLabel(source) {
  if (source === null || source === undefined || source === '') return '—'
  return SOURCE_LABELS[source] ?? String(source)
}

/** 鎖定理由**全文**，沒有就 null。降級七顆的 compose 自救路徑就寫在這裡面，
 *  截一個字都會讓管理員少一條活路。 */
export function lockedReasonText(item) {
  const reason = item?.locked_reason
  return typeof reason === 'string' && reason !== '' ? reason : null
}

/**
 * 一列要並排出來的五格。順序固定，讓四區的表格對得起來。
 *
 * A 類回空陣列：後端連 default 都遮了，畫面上生不出任何一格。
 */
export function valueCells(item) {
  if (!item || !VALUE_BEARING.has(item.class)) return []
  const hasPending = item.pending != null
  const storedBroken = item.stored != null && item.stored_usable === false

  return [
    {
      field: 'effective',
      label: '現在生效',
      text: formatSettingValue(item.effective),
      className: 'setting-cell--effective',
    },
    {
      field: 'pending',
      label: hasPending ? '重啟後生效（尚未套用）' : '重啟後生效',
      text: formatSettingValue(item.pending),
      // 有 pending 與沒 pending 是兩個樣式，而且都不是 effective 的樣式。
      className: hasPending ? 'setting-cell--pending' : 'setting-cell--empty',
    },
    {
      field: 'stored',
      label: storedBroken ? 'DB 存的原始字串（讀不回來，不會生效）' : 'DB 存的原始字串',
      text: formatSettingValue(item.stored),
      className: storedBroken ? 'setting-cell--unusable' : 'setting-cell--stored',
    },
    {
      field: 'default',
      label: '程式預設',
      text: formatSettingValue(item.default),
      className: 'setting-cell--default',
    },
    {
      field: 'source',
      label: '來源',
      text: sourceLabel(item.source),
      className: 'setting-cell--source',
    },
  ]
}

/**
 * 後端說有幾顆、這一頁收到幾顆 —— 對不起來就講出來。
 *
 * ⚠ 自查補的：沒有這一條，一個「少畫了六顆設定」的頁面在畫面上與正常的
 * 頁面**一模一樣**（每一區都有東西、沒有錯誤訊息）。那正是這一包要消滅的
 * 形狀：靜默地少講一件事。
 */
export function countMismatchWarning(overview) {
  const total = overview?.total
  if (typeof total !== 'number') return null
  const received = Array.isArray(overview.items) ? overview.items.length : 0
  if (received === total) return null
  return `後端說有 ${total} 顆設定，這一頁只收到 ${received} 顆 —— 下面不是全部。`
}

/** A 類唯一看得到的事實：有沒有人設過。後端沒講（null）就說沒講。 */
export function isSetLabel(item) {
  if (item?.is_set === true) return '已設定'
  if (item?.is_set === false) return '未設定'
  return '—'
}

/**
 * 編輯框裡要放的**可送出字串** —— 不是顯示字串。
 *
 * ⚠ 不可以用 `formatSettingValue()` 灌這個框：那邊的 `—`／`（空字串）` 是講給
 * 人看的，一旦被送回後端就會變成一個真的字串值。
 */
export function draftValue(item) {
  const value = item?.pending ?? item?.effective
  return value === null || value === undefined ? '' : String(value)
}

/**
 * 頂部那條大字 banner。
 *
 * ⚠ 只看 `boot_override_load_failed`，**不看 `boot_override_applied_count`**：
 * 「快照宣稱套過、行程其實沒套」正是後端 §2 第三種分岔的形狀，那時候
 * applied_count 不是 0，而管理員最需要知道的就是這一次開機的覆蓋沒有生效。
 */
export function bootOverrideBanner(overview) {
  if (overview?.boot_override_load_failed !== true) return null
  const reason = overview.boot_override_failure_reason
  return {
    tone: 'danger',
    title: '這次開機沒有載入設定覆蓋',
    message: '標示「重啟後生效」的值，這次開機一顆都沒有套上去 —— 現在跑的是 env／compose 或程式預設。',
    // 後端只給例外的類別名（細節刻意只進 log）。沒給就說沒給，不要編一個原因。
    reason: typeof reason === 'string' && reason !== '' ? reason : '後端沒有給原因（細節在 csp 的 log 裡）',
  }
}

/**
 * 存完之後要說的那一句。判準來自**回應那一列**，不是前端記得的 class：
 * `pending` 有值就是鐵證（這個值還沒生效），`restart_required` 是宣告。
 */
export function saveNotice(row) {
  if (row?.pending != null || row?.restart_required === true) {
    return { tone: 'warn', message: RESTART_HINT }
  }
  return { tone: 'ok', message: SAVED_NOW_HINT }
}

/** 後端的 `detail` **原樣**帶出來（值域說明與 locked_reason 都在裡面）。 */
export function extractDetail(error, fallback = '操作失敗') {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string' && detail !== '') return detail
  if (detail !== null && detail !== undefined) {
    try {
      return JSON.stringify(detail)
    } catch {
      return String(detail)
    }
  }
  return error?.message || fallback
}

/**
 * PUT 回來之後：那一列**整列**換掉。
 *
 * 刻意不是 `{ ...old, ...next }` —— merge 會讓回應裡消失的欄位（例如 pending
 * 被清掉）留著舊值，畫面就會繼續宣稱「重啟後生效」。清單裡沒有的 key 不硬塞，
 * 那代表前端手上的總覽已經過期，該重新載入而不是自己補一列。
 */
export function replaceRow(items, next) {
  const rows = Array.isArray(items) ? items : []
  return rows.map((item) => (item.key === next?.key ? next : item))
}

/** 初載狀態。**失敗不可以收斂成空清單** —— DepartmentsView 缺的就是這一格。 */
export function overviewState({ loaded, error, items }) {
  if (error) return 'failed'
  if (!loaded) return 'loading'
  return (Array.isArray(items) ? items.length : 0) === 0 ? 'empty' : 'ready'
}

const STATE_MESSAGES = {
  loading: '讀取中…',
  failed: '讀不到設定總覽 —— 下面不是「沒有設定」，是這一次沒問到。',
  empty: '後端回了零筆設定 —— 這一次真的沒有東西，不是讀取失敗。',
  ready: '',
}

export function overviewStateMessage(state) {
  return STATE_MESSAGES[state] ?? ''
}
