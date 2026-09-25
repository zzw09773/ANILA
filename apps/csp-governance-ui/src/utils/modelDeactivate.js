// 停用確認。有對話還綁著這個模型時要說出數量，而且不會自動改綁。

const BASE = '停用此模型？之後可透過該列的「啟用」按鈕重新啟用。'

/**
 * @param {{ router_conversation_count?: number }|null|undefined} model
 * @returns {{ message: string, confirmText: string, danger: boolean }}
 */
export function deactivateConfirm(model) {
  const count = Number(model?.router_conversation_count)
  if (!Number.isFinite(count) || count <= 0) {
    return { message: BASE, confirmText: '停用', danger: true }
  }
  return {
    danger: true,
    confirmText: '停用',
    message:
      `停用此模型？目前有 ${count} 段對話使用它。` +
      '停用後那些對話需要改選其他模型，系統不會自動改綁。' +
      '之後可透過該列的「啟用」按鈕重新啟用。',
  }
}
