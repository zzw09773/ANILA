// 既有的 in-app 通知。交接與完整回答都寫進同一張表、同一組 API。

export function listNotifications(authRequest, { unreadOnly = false } = {}) {
  const qs = unreadOnly ? "?unread_only=true" : "";
  return authRequest(`/api/notifications${qs}`, { method: "GET" });
}

export function markNotificationRead(authRequest, notifId) {
  return authRequest(`/api/notifications/${notifId}/read`, { method: "PUT" });
}

export function isAnswerNotification(row) {
  return row?.type === "answer_ready" || row?.type === "answer_failed";
}
