export const pending = []
export function getAlertSummary() {
  return new Promise((resolve, reject) => pending.push({ resolve, reject }))
}
