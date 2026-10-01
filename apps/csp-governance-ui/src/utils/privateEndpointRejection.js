/** 私有 IP 端點被拒、且缺的是信任主機時，表單上的那一列動作。 */

export const TRUST_HOST_NOTE = '註冊模型時加入'
export const TRUST_HOST_BUTTON_LABEL = '加入信任主機並重試'
export const TRUST_HOST_LINK_LABEL = '前往信任主機'
export const TRUST_HOST_LINK = '/trusted-hosts'
export const HOST_NOT_TRUSTED = 'host_not_trusted'

/**
 * @param {unknown} detail 後端 `detail`。字串（環境開關沒開）回 null。
 * @param {{ canManageTrustedHosts?: boolean }} [opts]
 * @returns {null | {
 *   message: string,
 *   host: string,
 *   canManage: boolean,
 *   buttonLabel: string,
 *   note: string,
 *   linkTo: string,
 *   linkLabel: string,
 * }}
 */
export function privateEndpointAction(detail, { canManageTrustedHosts = false } = {}) {
  if (!detail || typeof detail !== 'object') return null
  if (detail.code !== HOST_NOT_TRUSTED) return null
  if (typeof detail.host !== 'string' || !detail.host) return null
  const message = typeof detail.message === 'string' && detail.message.trim()
    ? detail.message
    : `主機 ${detail.host} 還不在信任主機清單`
  return {
    message,
    host: detail.host,
    canManage: !!canManageTrustedHosts,
    buttonLabel: TRUST_HOST_BUTTON_LABEL,
    note: TRUST_HOST_NOTE,
    linkTo: TRUST_HOST_LINK,
    linkLabel: TRUST_HOST_LINK_LABEL,
  }
}
