/**
 * 可測試的輪詢器（W3-3④）。
 *
 * 為什麼不直接在元件裡寫 `setInterval`
 * ------------------------------------
 * 因為那樣**沒有東西會抓到忘記清 timer**。忘記清的症狀不是壞掉的畫面，是
 * 使用者離開警報頁之後，那支 `setInterval` 仍然每 30 秒對 `/api/alerts` 發
 * 一次請求，直到整頁被重新載入為止；換頁十次就疊十支。air-gapped 內網的
 * CSP 沒有多餘的餘裕去吃這種洩漏，而且它在畫面上完全看不出來。
 *
 * 所以輪詢做成可注入 timer 的小工廠：`start` / `stop` 是明確的成對操作，
 * 元件只要 `onMounted(start)` + `onUnmounted(stop)`，而測試可以用假 timer
 * 直接斷言「間隔是 30 秒」與「stop 真的清掉了」。
 */

/** 警報輪詢間隔（毫秒）。規格明寫 30s。 */
export const ALERT_POLL_INTERVAL_MS = 30_000

/**
 * @param {() => unknown} task 每次到期要跑的事情（可回 Promise，錯誤自行處理）
 * @param {object} [options]
 * @param {number} [options.intervalMs]
 * @param {Function} [options.setTimer] 預設 setInterval（測試注入假的）
 * @param {Function} [options.clearTimer] 預設 clearInterval
 */
export function createPoller(task, options = {}) {
  const {
    intervalMs = ALERT_POLL_INTERVAL_MS,
    setTimer = setInterval,
    clearTimer = clearInterval,
  } = options

  if (typeof task !== 'function') {
    throw new TypeError('createPoller 需要一個函式')
  }

  let handle = null

  return {
    intervalMs,
    get running() {
      return handle !== null
    },
    /** 重複呼叫是 no-op —— 否則每次 start 都疊一支 timer。 */
    start() {
      if (handle !== null) return
      handle = setTimer(task, intervalMs)
    },
    /** 冪等：沒在跑時呼叫也安全（元件銷毀的順序不該是呼叫端要煩的事）。 */
    stop() {
      if (handle === null) return
      clearTimer(handle)
      handle = null
    },
  }
}
