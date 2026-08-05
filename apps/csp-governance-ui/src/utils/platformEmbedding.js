// 平台主 embedding 的指定決策 —— 純函式，`ModelsView` 只負責呼叫。
//
// 為什麼要把它抽出來
// ------------------
// 後端 `POST /api/models/{id}/set-platform-embedding` 回的
// `index_mismatch_warning` 一度是「送了但沒人收」的欄位：後端寫了、
// ModelsView 只讀 `truncation_warning` 與 `measured_native_dim`，
// 於是把整個知識庫索引作廢的管理員，看到的是一個綠色的成功提示。
// 那正是 FAKE-CONTROLS 清單上那種「按了、沒報錯、什麼也沒發生」。
//
// 抽成純函式之後，「什麼情況要用什麼語氣講什麼話」變成可以直接斷言的東西，
// 而不是藏在一個沒有 vitest 的 .vue 裡。`tests/platformEmbedding.test.mjs`
// 另外用原始碼層護欄釘住 ModelsView 真的有呼叫它 —— 純函式全綠而呼叫端忘了接，
// 正是本包一開始踩的那個坑。

/** 索引不一致的提示要停留多久（ms）。預設 3600 對「整個語料庫檢索不到」太短。 */
export const MISMATCH_TOAST_MS = 15000

/**
 * 從模型清單找出目前的平台主 embedding。
 * @param {Array<{id:number,name:string,display_name?:string,is_platform_embedding?:boolean}>} models
 */
export function currentPlatformEmbedding(models) {
  if (!Array.isArray(models)) return null
  return models.find((m) => m && m.is_platform_embedding) || null
}

/**
 * 指定前要不要先攔一下，以及攔的時候要說什麼。
 *
 * 只有在「換一個模型」時才攔；重新確認同一個模型不該有摩擦（本檔案所在的
 * ModelsView 對停用／取消主模型／永久刪除都是先 confirm，指定主 embedding
 * 反而是這一組動作裡唯一沒有確認的，而它的後果最大）。
 *
 * @returns {{ needed: boolean, message: string, confirmText: string, danger: boolean }}
 */
export function designationConfirm(models, targetId) {
  const list = Array.isArray(models) ? models : []
  const target = list.find((m) => m && m.id === targetId) || null
  const current = currentPlatformEmbedding(list)
  const targetName = target ? (target.display_name || target.name) : ''
  if (!current || !target || current.id === target.id) {
    return { needed: false, message: '', confirmText: '', danger: false }
  }
  const currentName = current.display_name || current.name
  return {
    needed: true,
    danger: true,
    confirmText: '仍要改為主 embedding',
    message:
      `要把平台主 embedding 由「${currentName}」改為「${targetName}」嗎？\n\n` +
      '檢索只會取用以現行模型建立索引的段落。改完之後，先前以舊模型索引的知識庫' +
      '在重新索引前都檢索不到內容（搜尋會明確回報索引模型不一致，不會靜靜地回空結果）。',
  }
}

/**
 * 指定成功後要顯示哪一則提示。
 *
 * 順序就是嚴重度：索引不一致 > 維度截斷 > 一般成功。最上面那一行是這個檔案
 * 存在的理由 —— 少了它，把整個語料庫作廢的操作會落回綠色成功提示。
 *
 * @param {{index_mismatch_warning?:string|null, truncation_warning?:string|null,
 *          measured_native_dim?:number|null, stranded_collections?:string[]}} data
 * @returns {{ message: string, tone: string, duration?: number }|null}
 */
export function designationToast(data) {
  const d = data || {}
  if (d.index_mismatch_warning) return { message: d.index_mismatch_warning, tone: 'warn', duration: MISMATCH_TOAST_MS }
  if (d.truncation_warning) return { message: d.truncation_warning, tone: 'warn' }
  if (d.measured_native_dim) {
    return {
      message: `已設為平台主 embedding（探測原生維度 ${d.measured_native_dim}）`,
      tone: 'ok',
    }
  }
  return null
}
