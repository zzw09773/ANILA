import { computed, onMounted, onBeforeUnmount, ref } from 'vue'

// 草稿的擁有者只在第一次 catalog 成功時綁定，後續讀取不能改綁另一個帳號。
export function useDraftActor() {
  const actorId = ref(null)
  const identityChanged = ref(false)
  const ready = computed(() => actorId.value !== null)
  const identityMessage = computed(() => identityChanged.value
    ? '登入身分已變更（IDENTITY_CHANGED）。這份草稿已保留並鎖定，不能以另一個帳號儲存或送出；請先回到原帳號，再另開助手快製頁處理。'
    : '')
  function observeActor(value) {
    if (actorId.value !== null && String(value) !== String(actorId.value)) identityChanged.value = true
  }
  function captureCatalog(catalog) {
    const value = catalog?.actor_user_id
    if (!Number.isInteger(value) || value <= 0) {
      const error = new Error('無法確認草稿所屬身分，請重新整理後再試（ACTOR_UNAVAILABLE）')
      error.code = 'ACTOR_UNAVAILABLE'
      throw error
    }
    if (actorId.value === null) actorId.value = value
    else observeActor(value)
    return !identityChanged.value
  }
  function lockIdentity() { identityChanged.value = true }
  function mutationOptions() {
    if (!ready.value || identityChanged.value) {
      const error = new Error(identityMessage.value || '尚未確認登入身分，請等待載入完成（ACTOR_UNAVAILABLE）')
      error.code = identityChanged.value ? 'IDENTITY_CHANGED' : 'ACTOR_UNAVAILABLE'
      throw error
    }
    return { headers: { 'X-ANILA-Actor-Id': String(actorId.value) } }
  }
  function onIdentity(event) { observeActor(event.detail?.userId) }
  onMounted(() => window.addEventListener('anila:identity', onIdentity))
  onBeforeUnmount(() => window.removeEventListener('anila:identity', onIdentity))
  return { actorId, identityChanged, ready, identityMessage, captureCatalog, lockIdentity, mutationOptions }
}
