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

/** 同一輪用 index 更新；續答把 base 加上去，新階段接在後面。 */
export function applyThinkingStage(stages, event, { base = 0, now = Date.now() } = {}) {
  const list = Array.isArray(stages) ? stages.map((row) => ({ ...row })) : [];
  const rawIndex = Number(event?.index);
  const title = typeof event?.title === "string" ? event.title.trim() : "";
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
  return raw
    .filter((row) => row && typeof row.title === "string" && row.title.trim())
    .map((row) => ({ ...row, title: row.title.trim() }));
}

/** 現場清單比較長（續答接上的）就留現場；否則用伺服器的終態，時間沿用現場。 */
export function mergeThinkingStages(live, incoming) {
  const next = readThinkingStages({ thinking_stages: incoming });
  const prev = Array.isArray(live) ? live : [];
  if (!next.length) return prev;
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
