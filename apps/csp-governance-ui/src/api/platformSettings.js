import client from './client'

// 平台設定總覽：十二顆 C 類設定一次回完（`{total, items[]}`）。
// 值與來源出自**同一個** payload —— 不可以為了補一欄再發第二支請求，
// 那正是後端自查時挖出來的「一次解析拿兩個答案」那個形狀的前端版。
export const getPlatformSettingsOverview = () =>
  client.get('/api/platform-settings/overview')

// 單顆寫入。回應**就是**那一顆的整列（含改完之後的 effective），
// 所以呼叫端拿回應整列取代，不要自己拼一列出來。
//
// ⚠ 送的是使用者原原本本打的那個字串；解析與值域由後端註冊表負責。
export const updatePlatformSetting = (key, value) =>
  client.put(`/api/platform-settings/${encodeURIComponent(key)}`, { value })
