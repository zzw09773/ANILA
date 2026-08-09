import client from './client'

// 平台設定總覽：96 顆一次回完（`{total, boot_override_*, items[]}`）。
// 值與來源出自**同一個** payload —— 不可以為了補一欄再發第二支請求，
// 那正是後端自查時挖出來的「一次解析拿兩個答案」那個形狀的前端版。
export const getPlatformSettingsOverview = () =>
  client.get('/api/platform-settings/overview')

// 單顆寫入。回應**就是**那一顆的整列（含改完之後的 effective／pending），
// 所以呼叫端拿回應整列取代，不要自己拼一列出來。
//
// ⚠ 送的是使用者原原本本打的那個字串：後端每一顆有自己的解析規則
// （`== "1"`、`!= "0"`、`lower() == "true"`…，其中一顆連 strip 都沒有），
// 前端先 trim 或先轉型，等於替它們發明第六種寫法。
export const updatePlatformSetting = (key, value) =>
  client.put(`/api/platform-settings/${encodeURIComponent(key)}`, { value })
