import { computed, onMounted, onBeforeUnmount, ref } from 'vue'
import { errorType } from '../api/errors'
import { clearActorDialogs, useDialog } from '../composables/useDialog.js'

export function useDraftActor() {
  const actorId = ref(null)
  const identityChanged = ref(false)
  const generation = ref(0)
  const reason = ref('IDENTITY_CHANGED')
  let disposed = false
  let expectedActor = null
  const { confirm } = useDialog()
  const ready = computed(() => actorId.value !== null && !identityChanged.value)
  const identityMessage = computed(() => identityChanged.value
    ? `${reason.value === 'ACCESS_CHANGED' ? '資料存取權已變更（ACCESS_CHANGED）' : '登入身分已變更（IDENTITY_CHANGED）'}。原畫面與未儲存內容已清除。請重新確認身分後，從伺服器載入已儲存的資料。`
    : '')
  function invalidate(nextReason) {
    if (identityChanged.value || disposed) return
    generation.value += 1
    clearActorDialogs()
    reason.value = nextReason
    actorId.value = null
    expectedActor = null
    identityChanged.value = true
  }
  function lockIdentity() { invalidate('IDENTITY_CHANGED') }
  function revokeAccess() { invalidate('ACCESS_CHANGED') }
  function observeActor(value) {
    if (identityChanged.value || disposed) return
    if (value === null || value === undefined) { lockIdentity(); return }
    const knownActor = actorId.value ?? expectedActor
    if (knownActor !== null && String(value) !== String(knownActor)) { lockIdentity(); return }
    if (Number.isSafeInteger(value) && value > 0) expectedActor = value
  }
  function captureCatalog(catalog) {
    if (identityChanged.value || disposed) return false
    const value = catalog?.actor_user_id
    if (!Number.isInteger(value) || value <= 0) {
      const error = new Error('無法確認目前身分，請重新載入（ACTOR_UNAVAILABLE）')
      error.code = 'ACTOR_UNAVAILABLE'
      throw error
    }
    if (expectedActor !== null && value !== expectedActor) { lockIdentity(); return false }
    if (actorId.value === null) actorId.value = value
    else observeActor(value)
    return !identityChanged.value
  }
  const clearedError = () => Object.assign(new Error('此畫面已清除，請重新確認身分（ACTOR_CLEARED）。'), {code:'ACTOR_CLEARED'})
  function current(token) { return !disposed && !identityChanged.value && token === generation.value }
  async function request(operation) {
    const token = generation.value
    if (!current(token)) throw clearedError()
    try {
      const response = await operation()
      if (!current(token)) throw clearedError()
      // Validate again when the awaiting caller reads its response. Identity can
      // change between the request continuation and the caller continuation.
      return response && typeof response === 'object' ? new Proxy(response, {
        get(target, key, receiver) {
          if (!current(token)) throw clearedError()
          return Reflect.get(target, key, receiver)
        }
      }) : response
    } catch (error) {
      if (!current(token)) throw clearedError()
      if (errorType(error, error?.code) === 'IDENTITY_CHANGED') lockIdentity()
      else if ([401,403,404].includes(error?.response?.status)) revokeAccess()
      throw error
    }
  }
  function protectApi(api) {
    return Object.fromEntries(Object.entries(api).map(([name, method]) => [name,
      typeof method === 'function' ? (...args) => request(() => method(...args)) : method]))
  }
  function mutationOptions() {
    if (!ready.value) throw identityChanged.value ? clearedError() : Object.assign(new Error('尚未確認身分（ACTOR_UNAVAILABLE）'), {code:'ACTOR_UNAVAILABLE'})
    return {headers:{'X-ANILA-Actor-Id':String(actorId.value)}}
  }
  async function confirmCurrent(options) {
    const token = generation.value
    if (!current(token)) return false
    const accepted = await confirm(options)
    return accepted === true && current(token)
  }
  const onPageHide = () => lockIdentity()
  const onPageShow = event => { if (event.persisted) lockIdentity() }
  function onIdentity(event) { observeActor(event.detail?.userId) }
  onMounted(() => {
    window.addEventListener('anila:identity', onIdentity)
    window.addEventListener('pagehide', onPageHide)
    window.addEventListener('pageshow', onPageShow)
  })
  onBeforeUnmount(() => {
    disposed = true
    generation.value += 1
    actorId.value = null
    expectedActor = null
    window.removeEventListener('anila:identity', onIdentity)
    window.removeEventListener('pagehide', onPageHide)
    window.removeEventListener('pageshow', onPageShow)
  })
  return {actorId, identityChanged, generation, ready, identityMessage, captureCatalog, lockIdentity, revokeAccess, mutationOptions, request, protectApi, current, confirmCurrent}
}
