/** 思考階段清單。Router 送 anila.thinking_stage，Shell 收成同一條時間軸。 */

export const THINKING_STAGE_MARK = {
  done: "✓",
  running: "⋯",
  error: "✕",
  stopped: "■",
};

function stageStatusOk(status) {
  return Object.prototype.hasOwnProperty.call(THINKING_STAGE_MARK, status);
}

/** 比對階段標題：前後空白去掉，連續空白收成一個。 */
function stageTitleKey(title) {
  return String(title || "").replace(/\s+/g, " ").trim();
}

/** 同一輪用 index 更新；續答把 base 加上去，新階段接在後面。 */
export function applyThinkingStage(stages, event, { base = 0, now = Date.now() } = {}) {
  const list = Array.isArray(stages) ? stages.map((row) => ({ ...row })) : [];
  const rawIndex = Number(event?.index);
  const title = typeof event?.title === "string" ? stageTitleKey(event.title) : "";
  const status = event?.status;
  if (!Number.isInteger(rawIndex) || rawIndex < 0 || !title || !stageStatusOk(status)) {
    return list;
  }
  const index = base + rawIndex;
  const found = list.find((row) => row.index === index);
  if (found) {
    found.title = title;
    found.status = status;
    if (status === "running" && typeof found.startedAt !== "number") found.startedAt = now;
    if (status !== "running") found.endedAt = now;
    return list;
  }
  // 同一則裡已經有這個標題就不要再加一筆，不管它正在跑還是已經結束。
  if (list.some((row) => stageTitleKey(row.title) === title)) {
    return list;
  }
  list.push({
    index,
    title,
    status,
    startedAt: now,
    ...(status !== "running" ? { endedAt: now } : {}),
  });
  list.sort((a, b) => a.index - b.index);
  return list;
}

/** 只改還在跑的那幾筆。完成、失敗、停止都不互相覆蓋。 */
export function settleThinkingStages(stages, status, now = Date.now()) {
  if (!Array.isArray(stages)) return [];
  if (!stageStatusOk(status) || status === "running") return stages;
  return stages.map((row) => (
    row && row.status === "running" ? { ...row, status, endedAt: now } : row
  ));
}

export function readThinkingStages(meta) {
  const raw = meta?.thinking_stages ?? meta?.thinkingStages;
  if (!Array.isArray(raw)) return [];
  const seen = new Set();
  const stages = [];
  for (const row of raw) {
    if (!row || typeof row.title !== "string") continue;
    const title = stageTitleKey(row.title);
    if (!title || seen.has(title)) continue;
    seen.add(title);
    stages.push({ ...row, title });
  }
  return stages;
}

/** 現場清單比較長（續答接上的）就留現場；否則用伺服器的終態，時間沿用現場。 */
export function mergeThinkingStages(live, incoming) {
  const next = readThinkingStages({ thinking_stages: incoming });
  if (!next.length) return Array.isArray(live) ? live : [];
  // 現場若因舊資料重複而比較長，先收成同一套標題再跟伺服器比。
  const prev = readThinkingStages({ thinking_stages: live });
  if (prev.length > next.length) return prev;
  return next.map((row) => {
    const old = prev.find((item) => item && item.index === row.index);
    if (!old) return row;
    return {
      ...row,
      startedAt: old.startedAt ?? row.startedAt,
      endedAt: row.endedAt ?? old.endedAt,
    };
  });
}
