// 使用者自訂文字 skill。送出時只帶 id，內容由 CSP 依 id 讀取。

export const SKILL_STATUS_LABEL = {
  draft: "草稿",
  pending: "待審",
  published: "已發布",
  rejected: "已退回",
  unpublished: "已下架",
};

export function skillChipLabel(name) {
  return `套用：${name}`;
}

export function appliedSkillLabel(name) {
  return `已自動套用：${name}`;
}

/** 游標所在的 `/查詢`。前面要是開頭或空白，中間不能有空格。 */
export function slashQuery(text, caret) {
  const source = String(text || "");
  const at = Math.max(0, Math.min(source.length, caret || 0));
  const match = /(^|\s)\/([^\s/]*)$/.exec(source.slice(0, at));
  if (!match) return null;
  return match[2];
}

export function filterSkills(skills, query) {
  const list = Array.isArray(skills) ? skills : [];
  const q = String(query || "").trim().toLowerCase();
  if (!q) return list;
  return list.filter((skill) => {
    const name = String(skill?.name || "").toLowerCase();
    const description = String(skill?.description || "").toLowerCase();
    return name.includes(q) || description.includes(q);
  });
}

/** 拿掉游標前那個 `/查詢`，保留前面的空白。 */
export function removeSlashToken(text, caret) {
  const source = String(text || "");
  const at = Math.max(0, Math.min(source.length, caret || 0));
  const match = /(^|\s)\/([^\s/]*)$/.exec(source.slice(0, at));
  if (!match) return source;
  const start = match.index + match[1].length;
  return source.slice(0, start) + source.slice(at);
}

export function skillHeader(skillId) {
  if (typeof skillId !== "number" || !Number.isInteger(skillId) || skillId <= 0) return null;
  return { "X-ANILA-Skill-Id": String(skillId) };
}

export async function listSkills(authRequest, view = "usable") {
  const data = await authRequest(`/api/skills?view=${encodeURIComponent(view)}`);
  return Array.isArray(data?.skills) ? data.skills : [];
}

export function createSkill(authRequest, body) {
  return authRequest("/api/skills", { method: "POST", body: JSON.stringify(body) });
}

export function updateSkill(authRequest, id, body) {
  return authRequest(`/api/skills/${id}`, { method: "PUT", body: JSON.stringify(body) });
}

export function deleteSkill(authRequest, id) {
  return authRequest(`/api/skills/${id}`, { method: "DELETE" });
}

export function submitSkill(authRequest, id, body) {
  return authRequest(`/api/skills/${id}/submit`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function listPublishTargets(authRequest) {
  return authRequest("/api/skills/publish-targets");
}
