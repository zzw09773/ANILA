// Promise-based in-app confirm() / toast(), replacing the blocking browser
// dialogs (window.confirm / window.alert / window.prompt) scattered across the
// admin views. Module-level singleton (same pattern as useTheme) so every
// caller and the single <TermDialogHost> share one reactive state.
//
//   const { confirm, toast } = useDialog()
//   if (!(await confirm('revoke?'))) return        // was: if (!confirm('revoke?')) return
//   toast(err.message, { tone: 'error' })          // was: alert(err.message)
//
// Typed-confirm (replaces window.prompt gate): pass requireText and the confirm
// button stays disabled until the user types the exact string.
//   if (!(await confirm({ message, requireText: link.name, danger: true }))) return
//
import { reactive } from 'vue'
import { currentIdentityEpoch, onIdentityInvalidated } from '../api/identityEpoch.js'

const emptyConfirm = () => ({
    id: 0,
    open: false,
    title: '請確認',
    message: '',
    confirmText: '確定',
    cancelText: '取消',
    danger: false,
    requireText: '',
    input: '',
})
const state = reactive({ confirm: emptyConfirm(), toasts: [] })

let resolver = null
let confirmSeq = 0
let observedActor = null
let toastSeq = 0
let actorGeneration = 0
const toastTimers = new Map()

function settle(value, instance = state.confirm) {
  if (instance !== state.confirm) return
  const r = resolver
  resolver = null
  Object.assign(state.confirm, emptyConfirm())
  state.confirm = emptyConfirm()
  if (r) r(value)
}

function confirm(opts) {
  const o = typeof opts === 'string' ? { message: opts } : (opts || {})
  return new Promise((resolve) => {
    // Never strand a previously pending confirm.
    if (resolver) settle(false)
    resolver = resolve
    state.confirm = {
      id: ++confirmSeq,
      open: true,
      title: o.title || '請確認',
      message: o.message || '',
      confirmText: o.confirmText || '確定',
      cancelText: o.cancelText || '取消',
      danger: !!o.danger,
      requireText: o.requireText || '',
      input: '',
    }
  })
}

function toast(message, opts = {}) {
  const id = ++toastSeq
  state.toasts.push({ id, message: String(message ?? ''), tone: opts.tone || 'info' })
  const duration = opts.duration ?? 3600
  const timer = setTimeout(() => {
    toastTimers.delete(id)
    const i = state.toasts.findIndex((t) => t.id === id)
    if (i !== -1) state.toasts.splice(i, 1)
  }, duration)
  toastTimers.set(id, timer)
}

export function clearActorDialogs() {
  actorGeneration += 1
  settle(false)
  for (const timer of toastTimers.values()) clearTimeout(timer)
  toastTimers.clear()
  state.toasts = []
}

onIdentityInvalidated(clearActorDialogs)
if (typeof window !== 'undefined') {
  window.addEventListener('pagehide', clearActorDialogs)
  window.addEventListener('anila:identity', event => {
    const actor = event.detail?.userId
    if (actor == null) { observedActor = null; clearActorDialogs(); return }
    if (!Number.isSafeInteger(actor) || actor <= 0) return
    if (observedActor !== null && actor !== observedActor) clearActorDialogs()
    observedActor = actor
  })
}

export function useDialog() {
  const generation = actorGeneration
  const epoch = currentIdentityEpoch()
  const current = () => generation === actorGeneration && epoch === currentIdentityEpoch()
  return {
    confirm: options => current() ? confirm(options) : Promise.resolve(false),
    toast: (message, options) => { if (current()) toast(message, options) },
  }
}

// Host-only internals (consumed by TermDialogHost.vue).
export const _dialogState = state
export const _settleConfirm = settle
