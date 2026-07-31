// 同仁通訊錄查詢。
//
// 擁有者裁決(OWNER-QUESTIONS Q9,2026-07-31,選項②):全院都查得到,
// 但後端只回姓名與單位。這裡刻意不做本地快取 —— 名冊會變,而且挑錯人
// 的代價(把對話交給不相干的同事)比多打一次 API 高。
export function searchDirectory(authRequest, query, { limit = 20 } = {}) {
  const params = new URLSearchParams();
  const q = (query || "").trim();
  if (q) params.set("q", q);
  params.set("limit", String(limit));
  return authRequest(`/api/directory/users?${params.toString()}`, { method: "GET" });
}

// 顯示用:「帳號 · 單位」。沒有單位的帳號不要留一個孤兒分隔點。
export function formatColleague(entry) {
  if (!entry) return "";
  const unit = (entry.department || "").trim();
  return unit ? `${entry.username} · ${unit}` : entry.username;
}
