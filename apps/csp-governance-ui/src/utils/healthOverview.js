/**
 * 服務健康總覽的純函式層（W3-3⑦）。
 *
 * 為什麼需要這一支
 * ----------------
 * 稽核 admin-journey D1：22 個治理視圖沒有**任何一個**顯示 router /
 * anila-studio / ingestion-worker / csp-db / redis / nginx / pptx-renderer 的
 * 狀態。管理者唯一的真實工具是 SSH 進平台主機跑 `anila-ops.sh health`。
 * 治理首頁的「服務健康」卡就是那件事的直接解，而這支模組是它的邏輯本體
 * ——刻意做成零 Vue／零 DOM 依賴的純函式，才能用 `node --test` 直接測。
 *
 * 五態字彙**沿用** `src/utils/healthStatus.js`（Slice 6b 就定好的那一套），
 * 不另發明。差別只有一處，而且是刻意的：
 *
 *   `unknown` 在模型清單頁是中性灰（「還沒探測過」不是警訊），在**基礎服務
 *   總覽**是黃燈。因為基礎服務的 unknown 意思是「我判斷不出來這個服務活著
 *   沒有」——那需要人看一眼，把它畫成灰的等於把它藏起來。
 */

// 明寫 `.js`:Vite 兩種寫法都吃,但 governance 的測試 runner 是 `node --test`
// (純 ESM 解析器),省副檔名會 ERR_MODULE_NOT_FOUND。
import { normalizeHealth, healthLabel } from './healthStatus.js'

export { normalizeHealth, healthLabel }

/** 總覽的紅綠燈：TermDot / TermStat 的 tone 字彙。 */
const OVERVIEW_TONES = {
  healthy: 'ok',
  degraded: 'warn',
  unknown: 'warn',
  unhealthy: 'danger',
  disabled: 'idle',
}

/**
 * 後端 `reason` 是 bounded 字彙（不是自由文字，見
 * `services/csp/app/services/health_checker.py` 的 PROBE_REASONS）。
 * 這裡是它的繁中對照；沒對到就原樣顯示，不臆測。
 */
export const PROBE_REASON_LABELS = {
  ok: '可連線',
  timeout: '探測逾時',
  unreachable: '無法連線',
  upstream_error: '服務回報錯誤',
  probe_failed: '探測失敗',
  not_deployed: '此部署未啟用',
  unsupported: '無法探測',
}

/** 狀態 → 紅綠燈 tone。未知字串一律回黃，不回綠（fail-safe）。 */
export function healthTone(status) {
  return OVERVIEW_TONES[normalizeHealth(status)] ?? 'warn'
}

/** bounded reason → 繁中。缺值回 '—'。 */
export function probeReasonLabel(reason) {
  if (!reason) return '—'
  return PROBE_REASON_LABELS[reason] ?? reason
}

/**
 * ISO 時間 → 本地可讀字串。無效／缺值回 '—'（絕不顯示 Invalid Date）。
 *
 * 刻意**不**併入 `utils/formatDate.js`：那是給「要顯示給人看的日期」用的
 * zh-TW 24h 樣式，這是**運維專用的 ISO 形狀**（`YYYY-MM-DD HH:mm:ss`，
 * 與 /health/overview 後端時間欄位同形、方便管理員對 log 對帳）。
 * 這裡手動 pad、不走 toLocaleString，**假設瀏覽器時區＝台北（內網環境事實）**。
 * @param {string|null|undefined} iso
 */
export function formatCheckedAt(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const pad = (n) => String(n).padStart(2, '0')
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  )
}

/**
 * 把 `/api/admin/health/overview` 的回應壓成卡片要用的形狀。
 *
 * 對 null / 半殘 payload 必須是安全的：載入中與載入失敗都會走這裡，而
 * 「載入失敗顯示全綠」是這張卡最糟的失效模式。所以缺資料一律 `unknown`。
 *
 * @param {object|null} payload
 */
export function summarizeHealthOverview(payload) {
  const rawServices = Array.isArray(payload?.services) ? payload.services : []
  const services = rawServices.map((svc) => {
    const status = normalizeHealth(svc?.status)
    return {
      name: svc?.name ?? '',
      label: svc?.label ?? svc?.name ?? '',
      kind: svc?.kind ?? '',
      status,
      statusLabel: healthLabel(status),
      tone: healthTone(status),
      reason: svc?.reason ?? '',
      reasonLabel: probeReasonLabel(svc?.reason),
      latencyMs: Number.isFinite(Number(svc?.latency_ms))
        ? Number(svc.latency_ms)
        : null,
    }
  })

  const counts = { healthy: 0, degraded: 0, unhealthy: 0, unknown: 0, disabled: 0 }
  for (const svc of services) counts[svc.status] += 1

  // overall 以後端算的為主；後端沒給（或還沒載入）就自己取最差，不預設健康。
  const overall = payload?.overall
    ? normalizeHealth(payload.overall)
    : worstStatus(services.map((s) => s.status))

  return {
    loaded: Boolean(payload),
    overall,
    overallLabel: healthLabel(overall),
    tone: healthTone(overall),
    checkedAt: payload?.checked_at ?? null,
    checkedAtLabel: formatCheckedAt(payload?.checked_at),
    services,
    counts,
    total: services.length,
    // 有紅或黃就要讓人看一眼；disabled 是刻意停用，不算。
    hasProblem: counts.unhealthy > 0 || counts.degraded > 0 || counts.unknown > 0,
    models: normalizeCounts(payload?.models),
    agents: normalizeCounts(payload?.agents),
  }
}

/** 五態計數（後端 DB 掛掉時回 null，這裡忠實傳遞 null，不補 0）。 */
function normalizeCounts(raw) {
  if (!raw) return null
  const pick = (k) => (Number.isFinite(Number(raw[k])) ? Number(raw[k]) : 0)
  return {
    total: pick('total'),
    unknown: pick('unknown'),
    healthy: pick('healthy'),
    degraded: pick('degraded'),
    unhealthy: pick('unhealthy'),
    disabled: pick('disabled'),
  }
}

const WORST_RANK = { healthy: 0, disabled: 0, unknown: 1, degraded: 2, unhealthy: 3 }

/** 取最差狀態。空清單 → 'unknown'（不是 healthy）。 */
export function worstStatus(statuses) {
  let worst = 'unknown'
  let rank = -1
  for (const raw of statuses ?? []) {
    const status = normalizeHealth(raw)
    const candidate = WORST_RANK[status] ?? 1
    if (candidate > rank) {
      rank = candidate
      worst = status
    }
  }
  return rank >= 0 ? worst : 'unknown'
}
