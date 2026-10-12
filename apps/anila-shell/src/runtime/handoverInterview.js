// 交接訪談的 API。題目與清單都由後端決定。
// 這裡不組清單，也沒有題庫。

export function mayRequestChecklist(interview) {
  return Boolean(
    interview
    && interview.status === "confirmed"
    && interview.checklist == null,
  );
}

export function listHandoverQuestions(authRequest) {
  return authRequest("/api/handover-interviews/questions", { method: "GET" });
}

export function listHandoverInterviews(authRequest) {
  return authRequest("/api/handover-interviews", { method: "GET" });
}

export function createHandoverInterview(authRequest, projectLabel) {
  return authRequest("/api/handover-interviews", {
    method: "POST",
    body: JSON.stringify({ project_label: projectLabel || "" }),
  });
}

export function saveHandoverAnswers(authRequest, interviewId, answers) {
  return authRequest(`/api/handover-interviews/${interviewId}/answers`, {
    method: "PUT",
    body: JSON.stringify({ answers }),
  });
}

export function confirmHandoverInterview(authRequest, interviewId) {
  return authRequest(`/api/handover-interviews/${interviewId}/confirm`, {
    method: "POST",
  });
}

export async function requestChecklist(authRequest, interview) {
  if (!mayRequestChecklist(interview)) {
    throw new Error("確認前不會產生交接清單。");
  }
  return authRequest(`/api/handover-interviews/${interview.id}/checklist`, {
    method: "POST",
  });
}

export function formatChecklistText(checklist) {
  if (!checklist || !Array.isArray(checklist.sections)) return "";
  const lines = ["交接清單"];
  const label = (checklist.project_label || "").trim();
  if (label) lines.push(`事項：${label}`);
  lines.push("");
  for (const section of checklist.sections) {
    if (section?.heading) lines.push(section.heading);
    for (const item of section?.items || []) {
      lines.push(`- ${item?.prompt || ""}`);
      lines.push(`  ${item?.note || ""}`);
    }
    lines.push("");
  }
  if (checklist.notice) lines.push(checklist.notice);
  return lines.join("\n").trim();
}
