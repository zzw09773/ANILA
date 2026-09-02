// 治理中心的錯誤網子（finding-no-error-boundary-20260820，MEDIUM）。
//
// 兩件一起（工單：只做 A 通過「有錯誤處理」的原則檢查、通不過驗收）：
//   B  ErrorPanel — 面板層 onErrorCaptured ＋ fallback 渲染：子樹在 render/setup 期
//      拋例外時，畫面上出現一句可讀訊息＋時間戳，而不是空白面板。
//   A  installErrorHandler — app.config.errorHandler 當 log 通道，根部 mount 就拋
//      的那種也不會整頁白。
// 內網鐵則：畫面不印 error.message、stack、路徑、主機名；那些只進 console。
// 沒有關聯碼：前端沒有把錯誤送到伺服器的通道，假的追蹤碼比沒有更糟。
//
// 寫成 .js（render function）而不是 .vue：治理中心的測試是 node:test 讀原始碼，
// 沒有 SFC loader；render function 元件可以直接被 @vue/server-renderer 真的渲染，
// 讓「會拋的元件真的拋、字由面板產生」這條驗收跑得起來。

import { defineComponent, h, onErrorCaptured, ref } from 'vue'

export function describeErrorForOperator(_error, at = new Date()) {
  return `系統發生錯誤，請重新整理或聯繫維運。時間：${at.toISOString()}`
}

export const ErrorPanel = defineComponent({
  name: 'ErrorPanel',
  setup(_props, { slots }) {
    const failedAt = ref(null)
    onErrorCaptured((error) => {
      console.error('[governance crash]', error)
      failedAt.value = new Date()
      return false // 已處理：不再往上冒、不讓 Vue 卸載整棵樹
    })
    const reload = () => {
      if (typeof window !== 'undefined') window.location.reload()
    }
    // 單一子節點時直接回傳它（不是包一層陣列）：AppLayout 用 <transition mode="out-in">
    // 包這個面板，Fragment 根節點不會跑 leave hook，換頁後畫面會永遠停在空白佔位。
    const passthrough = () => {
      const nodes = slots.default?.()
      return Array.isArray(nodes) && nodes.length === 1 ? nodes[0] : nodes
    }
    return () =>
      failedAt.value
        ? h('div', { role: 'alert', class: 'error-panel' }, [
            h('p', { class: 'error-panel__msg' }, describeErrorForOperator(null, failedAt.value)),
            h('button', { type: 'button', class: 'error-panel__btn', onClick: reload }, '重新整理'),
          ])
        : passthrough()
  },
})

export function installErrorHandler(app, logger = console) {
  app.config.errorHandler = (error, _instance, info) => {
    // 只是 log 通道：面板層才負責畫面。這裡吞掉例外，根部 mount 拋也不整頁白。
    logger.error('[governance error]', info, error)
  }
  return app
}
