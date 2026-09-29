// 代理管理員看得到的頁。用量只有全平台彙總，沒有每人明細與匯出。
export const DEPUTY_PAGES = ['/', '/users', '/alerts', '/feedback', '/banners', '/usage']

export function deputyMayOpen(path, { isDeputy, isAdmin }) {
  if (!(isDeputy && !isAdmin)) return true
  return DEPUTY_PAGES.includes(path)
}
