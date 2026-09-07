// 治理中心窄視窗導覽（≤900px 抽屜、≤520px 頂欄改用帳號選單）。
// 桌面維持固定側欄；窄螢幕不得把側欄堆在主內容上面（那會把 main 壓成一條縫）。
import { inject, onMounted, onUnmounted, provide, ref } from 'vue'

export const SHELL_NAV_KEY = Symbol('anila-shell-nav')
export const NARROW_MQ = '(max-width: 900px)'
export const COMPACT_MQ = '(max-width: 520px)'

function bindMql(mql, apply) {
  if (!mql) return () => {}
  apply(mql)
  if (typeof mql.addEventListener === 'function') {
    mql.addEventListener('change', apply)
    return () => mql.removeEventListener('change', apply)
  }
  if (typeof mql.addListener === 'function') {
    mql.addListener(apply)
    return () => mql.removeListener(apply)
  }
  return () => {}
}

export function provideShellNav() {
  const open = ref(false)
  const narrow = ref(false)
  const compact = ref(false)

  function close() { open.value = false }
  function toggle() { open.value = !open.value }

  let unbindNarrow = () => {}
  let unbindCompact = () => {}
  let unbindKey = () => {}

  onMounted(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    unbindNarrow = bindMql(window.matchMedia(NARROW_MQ), (e) => {
      narrow.value = Boolean(e.matches)
      if (!e.matches) open.value = false
    })
    unbindCompact = bindMql(window.matchMedia(COMPACT_MQ), (e) => {
      compact.value = Boolean(e.matches)
    })
    const onKey = (ev) => {
      if (ev.key === 'Escape' && open.value) close()
    }
    window.addEventListener('keydown', onKey)
    unbindKey = () => window.removeEventListener('keydown', onKey)
  })
  onUnmounted(() => {
    unbindNarrow()
    unbindCompact()
    unbindKey()
  })

  const api = { open, narrow, compact, toggle, close }
  provide(SHELL_NAV_KEY, api)
  return api
}

export function useShellNav() {
  const api = inject(SHELL_NAV_KEY, null)
  if (!api) {
    throw new Error('useShellNav() 必須在 provideShellNav() 之內呼叫')
  }
  return api
}
