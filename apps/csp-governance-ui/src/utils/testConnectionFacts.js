/**
 * Agent「測試連線」三事實的顯示對照（P2.1）。
 *
 * POST /api/agents/{id}/test-connection 回傳三個獨立事實，不可收成一盞綠／紅燈。
 * credentials_accepted / path_verified 的 null 表示「無法判定」——絕不可當成成功
 * （閘道常在路由前認証，錯誤路徑也會 401）。
 *
 * 純函式、零 Vue／DOM 依賴，供 `node --test` 釘住三態。
 */

/** @typedef {'ok'|'fail'|'unknown'} FactTone */

/**
 * 三態布林（含 null）→ 繁中標籤。
 * @param {boolean|null|undefined} value
 * @returns {'是'|'否'|'無法判定'}
 */
export function factLabel(value) {
  if (value === true) return '是'
  if (value === false) return '否'
  return '無法判定'
}

/**
 * 三態布林 → 語氣。null／undefined 必須是 unknown，不可當 ok。
 * @param {boolean|null|undefined} value
 * @returns {FactTone}
 */
export function factTone(value) {
  if (value === true) return 'ok'
  if (value === false) return 'fail'
  return 'unknown'
}

/**
 * 把後端 TestConnectionResponse 收成可渲染的三事實＋原文 detail。
 * detail 原樣帶過，前端不得自行拼 endpoint URL。
 *
 * @param {{
 *   host_reachable?: boolean,
 *   credentials_accepted?: boolean|null,
 *   path_verified?: boolean|null,
 *   status_code?: number|null,
 *   detail?: string,
 * }|null|undefined} result
 */
/**
 * 說明列不可空白：非物件 body（例如 SPA catch-all 的 HTML）或後端沒給 detail
 * 時，給使用者看得懂的後備文案。
 * @param {unknown} result
 * @returns {string}
 */
export function resolveTestConnectionDetail(result) {
  if (typeof result?.detail === 'string' && result.detail.trim() !== '') {
    return result.detail
  }
  if (result == null || typeof result !== 'object' || Array.isArray(result)) {
    return '回應不是預期的探測結果（可能收到 HTML 或其他非 JSON），三事實皆無法判定'
  }
  return '後端未提供說明文字'
}

export function formatTestConnectionFacts(result) {
  const host = result?.host_reachable
  const creds = result?.credentials_accepted
  const path = result?.path_verified
  return {
    facts: [
      { key: 'host_reachable', label: '主機可連線', value: host, display: factLabel(host), tone: factTone(host) },
      { key: 'credentials_accepted', label: '憑證被接受', value: creds, display: factLabel(creds), tone: factTone(creds) },
      { key: 'path_verified', label: '路徑正確', value: path, display: factLabel(path), tone: factTone(path) },
    ],
    detail: resolveTestConnectionDetail(result),
    statusCode: result?.status_code ?? null,
  }
}
