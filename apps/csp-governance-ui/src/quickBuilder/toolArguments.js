// 在送出前將畫面已呈現的預設值寫回資料；尤其必填的 false 不等於未填。
export function initializeToolArguments(schema, current = {}) {
  const next = { ...current }
  for (const [key, child] of Object.entries(schema.properties || {})) {
    const required = schema.required?.includes(key)
    if (next[key] === undefined) {
      if (child.default !== undefined) next[key] = JSON.parse(JSON.stringify(child.default))
      else if (required && child.type === 'boolean') next[key] = false
    }
    if (child.type === 'object' && (required || next[key] !== undefined)) {
      next[key] = initializeToolArguments(child, next[key] || {})
    }
    if (child.type === 'array' && Array.isArray(next[key]) && child.items?.type === 'object') {
      next[key] = next[key].map(value => initializeToolArguments(child.items, value))
    }
  }
  return next
}
