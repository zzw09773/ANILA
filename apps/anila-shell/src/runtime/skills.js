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

export function assistSkill(authRequest, body, signal) {
  return authRequest("/api/skills/assist", {
    method: "POST",
    body: JSON.stringify(body),
    signal,
  });
}

// ---- 技能疊加（X-ANILA-Skill-Ids）----

/** 治理中心沒給上限時的預設值，與後端 skills.max_stack 預設相同。 */
export const DEFAULT_MAX_SKILL_STACK = 3;

/** 正整數、去重、保留順序。 */
export function cleanSkillIds(ids) {
  const out = [];
  for (const raw of Array.isArray(ids) ? ids : []) {
    const id = typeof raw === "number" ? raw : Number(raw);
    if (!Number.isInteger(id) || id <= 0 || out.includes(id)) continue;
    out.push(id);
  }
  return out;
}

/**
 * 多個 skill 的標頭。新標頭 `X-ANILA-Skill-Ids` 照順序列出全部；同時保留舊的
 * `X-ANILA-Skill-Id`（第一個），還沒升級的後端至少套到第一個。後端有新標頭時
 * 不再讀舊標頭。
 */
export function skillHeaders(ids) {
  const clean = cleanSkillIds(ids);
  if (clean.length === 0) return null;
  return {
    "X-ANILA-Skill-Ids": clean.join(","),
    "X-ANILA-Skill-Id": String(clean[0]),
  };
}

/** 從 `GET /api/skills` 的回應讀疊加上限；沒有就用預設值。 */
export function maxStackFrom(data) {
  const candidates = [data?.max_stack, data?.limits?.max_stack, data?.settings?.max_stack];
  for (const value of candidates) {
    const n = Number(value);
    if (Number.isInteger(n) && n >= 1) return Math.min(n, 20);
  }
  return DEFAULT_MAX_SKILL_STACK;
}

export async function listUsableSkillsWithLimits(authRequest) {
  const data = await authRequest("/api/skills?view=usable");
  return {
    skills: Array.isArray(data?.skills) ? data.skills : [],
    maxStack: maxStackFrom(data),
  };
}

export function setSkillFavorite(authRequest, id, favorite) {
  return authRequest(`/api/skills/${id}/favorite`, { method: favorite ? "POST" : "DELETE" });
}

export function copySkill(authRequest, id) {
  return authRequest(`/api/skills/${id}/copy`, { method: "POST", body: "{}" });
}

const RECENT_LIMIT = 5;

/**
 * 「/」選單分組：釘選、最近用過、我的、單位、全院。每個 skill 只出現在第一個
 * 符合的組。回傳 [{ key, label, items }]，空組不列。
 */
export function groupSkills(skills, { recentLimit = RECENT_LIMIT } = {}) {
  const list = Array.isArray(skills) ? skills.filter((s) => s && Number.isInteger(s.id)) : [];
  const used = new Set();
  const take = (pred) => {
    const items = list.filter((s) => !used.has(s.id) && pred(s));
    items.forEach((s) => used.add(s.id));
    return items;
  };
  const pinned = take((s) => s.favorite === true);
  const recentCandidates = list
    .filter((s) => !used.has(s.id) && s.last_used_at)
    .sort((a, b) => String(b.last_used_at).localeCompare(String(a.last_used_at)))
    .slice(0, recentLimit);
  recentCandidates.forEach((s) => used.add(s.id));
  const mine = take((s) => s.scope === "personal" || !s.scope);
  const unit = take((s) => s.scope === "unit");
  const campus = take((s) => s.scope === "campus");
  const rest = take(() => true);
  return [
    { key: "pinned", label: "釘選", items: pinned },
    { key: "recent", label: "最近用過", items: recentCandidates },
    { key: "mine", label: "我的", items: mine },
    { key: "unit", label: "單位", items: unit },
    { key: "campus", label: "全院", items: [...campus, ...rest] },
  ].filter((g) => g.items.length > 0);
}

/**
 * 這則訊息實際套用的 skill 清單。新格式 `applied_skills`，舊對話只有
 * `applied_skill`（或事件頂層就是那一個）。
 */
export function appliedSkillList(skill) {
  if (!skill || typeof skill !== "object") return [];
  const many = Array.isArray(skill.applied_skills) ? skill.applied_skills : null;
  const list = many && many.length ? many : [skill.applied_skill && typeof skill.applied_skill === "object" ? skill.applied_skill : skill];
  return list.filter((s) => s && typeof s === "object" && typeof s.name === "string" && s.name);
}

function persistableSkill(skill) {
  return {
    id: skill.id,
    version_id: skill.version_id ?? skill.versionId ?? null,
    version: skill.version ?? null,
    name: skill.name,
    body: typeof skill.body === "string" ? skill.body : "",
    mode: skill.mode === "auto" ? "auto" : "manual",
  };
}

/** 落庫用：{ applied_skill, applied_skills }；沒套用回 null。 */
export function appliedSkillMeta(skill) {
  const list = appliedSkillList(skill).map(persistableSkill);
  if (list.length === 0) return null;
  return { applied_skill: list[0], applied_skills: list };
}

/** 讀回：舊對話只有 applied_skill，新的另有 applied_skills。 */
export function appliedSkillFromMeta(meta) {
  if (!meta || typeof meta !== "object") return null;
  const first = meta.applied_skill && typeof meta.applied_skill === "object" ? meta.applied_skill : null;
  const many = Array.isArray(meta.applied_skills) ? meta.applied_skills.filter((s) => s && typeof s === "object") : [];
  if (!first && many.length === 0) return null;
  const head = first || many[0];
  return many.length > 1 ? { ...head, applied_skills: many } : head;
}
