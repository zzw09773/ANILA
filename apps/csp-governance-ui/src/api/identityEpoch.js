const STORAGE_KEY = 'anila:identity-epoch'
const CHANNEL_NAME = 'anila:identity'

// 通知只撤銷本機展示狀態；目前身分仍須由 /me 與後端授權確認。
export function createIdentityEpochBridge(target) {
  let epoch = 0
  let channel = null
  const seen = new Set()
  const listeners = new Set()

  function announce(userId) {
    target.dispatchEvent(new target.CustomEvent('anila:identity', { detail: { userId } }))
  }

  function invalidate(reason = 'local') {
    epoch += 1
    announce(null)
    for (const listener of listeners) listener(reason)
  }

  function receive(value) {
    if (!value || value.version !== 1 || typeof value.epoch !== 'string'
      || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value.epoch)
      || Object.keys(value).length !== 2 || seen.has(value.epoch)) return
    seen.add(value.epoch)
    if (seen.size > 32) seen.delete(seen.values().next().value)
    invalidate('remote')
  }

  function storage(event) {
    if (event.key !== STORAGE_KEY || !event.newValue) return
    try { receive(JSON.parse(event.newValue)) } catch { /* 非通知內容不採用。 */ }
  }
  target.addEventListener('storage', storage)
  try {
    if (target.BroadcastChannel) {
      channel = new target.BroadcastChannel(CHANNEL_NAME)
      channel.onmessage = (event) => receive(event.data)
      channel.unref?.()
    }
  } catch { channel = null }

  return {
    current: () => epoch,
    announce,
    invalidate() {
      invalidate()
      let nonce
      try { nonce = target.crypto?.randomUUID?.() } catch { /* 可改用瀏覽器安全亂數。 */ }
      try {
        if (!nonce && target.crypto?.getRandomValues) {
          const bytes = target.crypto.getRandomValues(new Uint8Array(16))
          bytes[6] = (bytes[6] & 15) | 64
          bytes[8] = (bytes[8] & 63) | 128
          const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
          nonce = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
        }
      } catch { return }
      if (!nonce) return
      const value = { version: 1, epoch: nonce }
      seen.add(nonce)
      if (seen.size > 32) seen.delete(seen.values().next().value)
      try { target.localStorage.setItem(STORAGE_KEY, JSON.stringify(value)) } catch { /* 可改由同來源頻道通知。 */ }
      try { channel?.postMessage(value) } catch { /* 本分頁仍已立即停止交付。 */ }
    },
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    close() {
      target.removeEventListener('storage', storage)
      channel?.close()
      listeners.clear()
    },
  }
}

let browserBridge
function bridge() {
  if (typeof window === 'undefined') return null
  browserBridge ??= createIdentityEpochBridge(window)
  return browserBridge
}

export const currentIdentityEpoch = () => bridge()?.current() ?? 0
export const announceIdentity = (id) => bridge()?.announce(id)
export const invalidateIdentity = () => bridge()?.invalidate()
export const onIdentityInvalidated = (listener) => bridge()?.subscribe(listener) ?? (() => {})
