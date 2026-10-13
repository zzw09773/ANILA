// 完整回答：問題先落庫，模型在背景跑，完成後走既有 /api/notifications。

export function postBackgroundAnswer(
  authRequest,
  convId,
  { content, agentName, referenceIds, attachmentAdmitted } = {},
) {
  const body = { content };
  if (typeof agentName === "string" && agentName) body.agent_name = agentName;
  if (Array.isArray(referenceIds) && referenceIds.length > 0) {
    body.attachment_reference_ids = referenceIds;
  }
  if (attachmentAdmitted && typeof attachmentAdmitted === "object") {
    body.metadata = { attachment_admitted: attachmentAdmitted };
  }
  return authRequest(`/api/conversations/${convId}/background-answers`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}
