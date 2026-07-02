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

const state = reactive({
  confirm: {
    open: false,
    title: '請確認',
    message: '',
    confirmText: '確定',
    cancelText: '取消',
    danger: false,
    requireText: '',
    input: '',
  },
  toasts: [], // [{ id, message, tone }]
})

let resolver = null
let toastSeq = 0

function settle(value) {
  const r = resolver
  resolver = null
  state.confirm.open = false
  if (r) r(value)
}

function confirm(opts) {
  const o = typeof opts === 'string' ? { message: opts } : (opts || {})
  return new Promise((resolve) => {
    // Never strand a previously pending confirm.
    if (resolver) { const r = resolver; resolver = null; r(false) }
    resolver = resolve
    state.confirm = {
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
  setTimeout(() => {
    const i = state.toasts.findIndex((t) => t.id === id)
    if (i !== -1) state.toasts.splice(i, 1)
  }, duration)
}

export function useDialog() {
  return { confirm, toast }
}

// Host-only internals (consumed by TermDialogHost.vue).
export const _dialogState = state
export const _settleConfirm = settle
