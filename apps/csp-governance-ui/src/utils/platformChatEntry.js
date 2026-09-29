/** 治理中心把 anila-router 當成平台對話入口，不當成可編輯的一般模型。 */

export const PLATFORM_CHAT_ENTRY_NAME = 'anila-router'

export const PLATFORM_CHAT_ENTRY_TITLE = '平台對話入口：ANILA'

export const PLATFORM_CHAT_ENTRY_EXPLANATION =
  '平台對話入口，用哪個模型回答由上方主要模型決定；不是助手，也不能當一般模型編輯'

export function isPlatformChatEntry(model) {
  return Boolean(model && model.name === PLATFORM_CHAT_ENTRY_NAME)
}

export function ordinaryModels(models) {
  return (Array.isArray(models) ? models : []).filter(
    (model) => model && !isPlatformChatEntry(model),
  )
}

export function platformChatEntryCard(models) {
  const list = Array.isArray(models) ? models : []
  const entry = list.find((model) => isPlatformChatEntry(model)) || null
  const primary = list.find(
    (model) => model && model.is_router_primary && !isPlatformChatEntry(model),
  ) || null
  let statusLabel = '尚未建立'
  if (entry) statusLabel = entry.is_active ? '啟用' : '停用'
  return {
    title: PLATFORM_CHAT_ENTRY_TITLE,
    explanation: PLATFORM_CHAT_ENTRY_EXPLANATION,
    statusLabel,
    active: Boolean(entry && entry.is_active),
    mainModelLabel: primary ? (primary.display_name || primary.name) : '尚未設定',
  }
}
