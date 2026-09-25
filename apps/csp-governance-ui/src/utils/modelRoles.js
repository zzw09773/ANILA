/** 治理中心模型角色面板的純函式。 */

export function eligibleRoleModels(models, acceptedTypes) {
  const types = new Set(acceptedTypes || [])
  return (Array.isArray(models) ? models : []).filter(
    (model) =>
      model &&
      model.is_active &&
      model.name !== 'anila-router' &&
      types.has(model.model_type),
  )
}

export function roleWarning(role) {
  if (!role || role.status === 'ok') return ''
  return role.message || '尚未設定'
}

/** 用使用者自己的憑證呼叫的角色：有沒有全院授權，以及要不要警告。 */
export function roleAudience(role) {
  if (!role || !role.end_user_credential || !role.model) return null
  const granted = role.all_users_grant === true
  return {
    granted,
    warning: granted
      ? ''
      : '此模型尚未授權給所有使用者。沒有這項授權的人，用這個角色會被拒絕。',
  }
}
