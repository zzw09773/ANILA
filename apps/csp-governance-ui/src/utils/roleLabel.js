// 角色代號 → 中文稱謂。畫面上給人看的一律用這裡，不直接印 owner/admin。
export const ROLE_LABELS = {
  owner: '擁有者',
  admin: '管理員',
  developer: '開發者',
  user: '使用者',
  system: '系統',
  guest: '訪客',
}

export function roleLabel(role) {
  return ROLE_LABELS[role] ?? (role || '訪客')
}
