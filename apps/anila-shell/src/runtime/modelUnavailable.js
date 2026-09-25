// 對話綁定的模型不能再用時，給使用者的說明。文案只放這裡，畫面不要自己組。

const NOTICE = {
  inactive: (name) => `這段對話使用的模型「${name}」已停用，請改選其他模型後再送出。`,
  missing: (name) => `這段對話使用的模型「${name}」已不存在，請改選其他模型後再送出。`,
  not_router_enabled: (name) => `這段對話使用的模型「${name}」已不能在對話裡選用，請改選其他模型後再送出。`,
  not_granted: (name) => `這段對話使用的模型「${name}」已不在你的使用範圍，請改選其他模型後再送出。`,
};

export function modelUnavailableLabel(displayName, name) {
  const display = typeof displayName === "string" ? displayName.trim() : "";
  if (display) return display;
  const slug = typeof name === "string" ? name.trim() : "";
  return slug || "未知模型";
}

export function modelUnavailableNotice(reason, displayName, name) {
  const label = modelUnavailableLabel(displayName, name);
  const write = NOTICE[reason] || NOTICE.inactive;
  return write(label);
}

/** anila.error 或 HTTP detail。不是這個錯誤就回 null。 */
export function modelUnavailableFromPayload(payload) {
  if (!payload || typeof payload !== "object" || payload.code !== "model_unavailable") {
    return null;
  }
  const reason = typeof payload.reason === "string" ? payload.reason : "inactive";
  const displayName = payload.display_name || payload.displayName || "";
  const name = payload.name || "";
  return {
    code: "model_unavailable",
    reason,
    displayName: modelUnavailableLabel(displayName, name),
    message: modelUnavailableNotice(reason, displayName, name),
  };
}

export function readModelUnavailableError(error) {
  if (!error || error.code !== "model_unavailable") return null;
  const fromPayload = modelUnavailableFromPayload({
    code: error.code,
    reason: error.reason,
    display_name: error.displayName,
    name: error.name,
  });
  if (!fromPayload) return null;
  return fromPayload;
}

export function noticeForBoundModel(conv) {
  if (!conv || !conv.routerModelUnavailableReason) return "";
  return modelUnavailableNotice(
    conv.routerModelUnavailableReason,
    conv.routerModelDisplayName || conv.routerModelName,
  );
}
