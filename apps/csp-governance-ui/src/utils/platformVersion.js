/** 儀表板顯示的平台版本。空的當成未知，不要顯示成空白。 */
export function platformVersionLabel(version) {
  const text = String(version ?? '').trim()
  return text || '未知'
}
